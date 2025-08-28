import json
import os
import logging
from datetime import datetime, date
from typing import Literal, Optional, Dict
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from dateutil import parser as date_parser
import psycopg2

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Memory setup
memory = MemorySaver()

# Azure OpenAI LLM
llm = AzureChatOpenAI(
    azure_endpoint=os.getenv('AZURE_OPENAI_ENDPOINT'),
    openai_api_version=os.getenv('AZURE_API_VERSION'),
    azure_deployment=os.getenv('AZURE_DEPLOYMENT_NAME'),
    openai_api_key=os.getenv('AZURE_OPENAI_API_KEY'),
    temperature=0.5
)

def get_db_connection():
    try:
        connection_string = os.getenv("DATABASE_URL")
        if not connection_string:
            raise ValueError("DATABASE_URL environment variable is not set")
        return psycopg2.connect(connection_string)
    except psycopg2.OperationalError as e:
        print(f"Database connection failed: {e}")
        return None

# Delay database connection until needed
connection = None

def get_db_connection_safe():
    global connection
    if connection is None:
        connection = get_db_connection()
    return connection

def missing_param_response(param):
    param_natural = param.replace("_", " ").capitalize()
    return f"{param_natural} is missing. Could you please provide it?"

def parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string flexibly, assuming current or next year if not specified."""
    try:
        parsed = date_parser.parse(date_str, dayfirst=False)
        current_year = datetime.now().year
        if parsed.year < current_year:
            parsed = parsed.replace(year=current_year if parsed.date() >= date.today() else current_year + 1)
        return parsed
    except ValueError:
        return None

### Tool Definitions ###

@tool
def check_room_availability(check_in_date: str = None, check_out_date: str = None):
    """
    Check room availability for a given date range.

    Args:
        check_in_date (str): Check-in date in any recognizable format (e.g., YYYY-MM-DD, 18th August).
        check_out_date (str): Check-out date in any recognizable format.

    Returns:
        str: Available room numbers with room types or unavailability message.
    """
    if not check_in_date:
        return missing_param_response("check_in_date")
    if not check_out_date:
        return missing_param_response("check_out_date")
    
    check_in = parse_date(check_in_date)
    check_out = parse_date(check_out_date)
    if not check_in or not check_out:
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or 18th August."
    if check_in.date() < date.today():
        return f"Check-in date {check_in.strftime('%dth %B %Y').replace('0th', 'th')} is in the past. Please provide a future date."
    if check_out <= check_in:
        return "Check-out date must be after check-in date."
    
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT room_number, room_type
                FROM public.dh_rooms
                WHERE room_number NOT IN (
                    SELECT room_number
                    FROM public.dh_bookings
                    WHERE (check_in_date <= %s AND check_out_date >= %s)
                );
            """, (check_out.strftime("%Y-%m-%d"), check_in.strftime("%Y-%m-%d")))
            rooms = cursor.fetchall()
            if not rooms:
                return f"No rooms available between {check_in.strftime('%dth %B %Y').replace('0th', 'th')} and {check_out.strftime('%dth %B %Y').replace('0th', 'th')}."
            room_list = [f"Room {row[0]} ({row[1]})" for row in rooms]
            return f"Available rooms between {check_in.strftime('%dth %B %Y').replace('0th', 'th')} and {check_out.strftime('%dth %B %Y').replace('0th', 'th')}:\n" + ", ".join(room_list)
    except Exception as e:
        return f"Error checking room availability: {str(e)}"

@tool
def book_room(guest_name: str = None, room_number: str = None, check_in_date: str = None, check_out_date: str = None, session_id: str = None):
    """
    Book a room for a guest, automatically calculating the total amount based on room type and stay duration.

    Args:
        guest_name (str): Name of the guest.
        room_number (str): Room number to book.
        check_in_date (str): Check-in date in any recognizable format.
        check_out_date (str): Check-out date in any recognizable format.
        session_id (str): Session ID to retrieve stored guest name.

    Returns:
        str: Confirmation of booking with dates in conversational format.
    """
    # Retrieve session-specific guest name if not provided
    if not guest_name and session_id:
        checkpoint = memory.get({"configurable": {"thread_id": session_id}})
        if checkpoint and 'customer_name' in checkpoint.get('user_config', {}):
            guest_name = checkpoint['user_config']['customer_name']
    
    if not guest_name:
        return missing_param_response("guest_name")
    if not room_number:
        return missing_param_response("room_number")
    if not check_in_date:
        return missing_param_response("check_in_date")
    if not check_out_date:
        return missing_param_response("check_out_date")
    
    check_in = parse_date(check_in_date)
    check_out = parse_date(check_out_date)
    if not check_in or not check_out:
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or 18th August."
    if check_in.date() < date.today():
        return f"Check-in date {check_in.strftime('%dth %B %Y').replace('0th', 'th')} is in the past. Please provide a future date."
    if check_out <= check_in:
        return "Check-out date must be after check-in date."
    
    try:
        nights = (check_out - check_in).days
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT room_type
                FROM public.dh_rooms
                WHERE room_number = %s;
            """, (room_number,))
            room = cursor.fetchone()
            if not room:
                return f"No room found with number {room_number}."
            
            room_type = room[0]
            cursor.execute("""
                SELECT rate_per_night
                FROM public.dh_roomtypes
                WHERE room_type = %s;
            """, (room_type,))
            rate = cursor.fetchone()
            if not rate:
                return f"No rate found for room type {room_type}."
            
            total_amount = rate[0] * nights
            cursor.execute("""
                INSERT INTO public.dh_bookings (guest_name, room_number, check_in_date, check_out_date, total_amount, booking_date)
                VALUES (%s, %s, %s, %s, %s, %s);
            """, (guest_name, room_number, check_in.strftime("%Y-%m-%d"), check_out.strftime("%Y-%m-%d"), total_amount, datetime.now()))
            conn.commit()
            
            # Update session memory with guest_name
            if session_id:
                checkpoint = memory.get({"configurable": {"thread_id": session_id}}) or {}
                user_config = checkpoint.get('user_config', {})
                user_config['customer_name'] = guest_name
                memory.save({"configurable": {"thread_id": session_id}}, {'user_config': user_config})
            
            check_in_str = check_in.strftime("%dth %B %Y").replace("0th", "th")
            check_out_str = check_out.strftime("%dth %B %Y").replace("0th", "th")
            return f"Room {room_number} booked for {guest_name} from {check_in_str} to {check_out_str} for ${total_amount:.2f}."
    except Exception as e:
        conn.rollback()
        return f"Error booking room {room_number}: {str(e)}"

@tool
def raise_guest_request(room_number: str = None, request_description: str = "Guest request"):
    """
    Raise a guest request ticket for a room.

    Args:
        room_number (str): Room number.
        request_description (str): Description of the guest request.

    Returns:
        str: Confirmation message with date in conversational format. Returns the ticket ID as well.
    """
    if not room_number:
        return missing_param_response("room_number")
    
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO public.dh_tickets (room_number, request_description, status, assigned_to_department, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            """, (room_number, request_description, "open", "housekeeping", datetime.now()))
            
            ticket_id = cursor.fetchone()[0]
            conn.commit()
            
            created_at = datetime.now().strftime("%dth %B %Y").replace("0th", "th")
            return f"Guest request ticket #{ticket_id} raised for room {room_number} : {request_description}."
    
    except Exception as e:
        conn.rollback()
        return f"Error raising guest request for room {room_number}: {str(e)}"


@tool
def view_guest_requests():
    """
    View all open guest request tickets.

    Returns:
        str: List of guest request ticket details with dates in conversational format.
    """
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, room_number, request_description, assigned_to_department, created_at
                FROM public.dh_tickets
                WHERE status = 'open'
                ORDER BY created_at DESC;
            """)
            tickets = cursor.fetchall()
            if not tickets:
                return "No open guest requests found."
            return "\n".join([
                f"Ticket #{ticket[0]} - Room {ticket[1]}: {ticket[2]} (Assigned to {ticket[3]}, Created at {ticket[4].strftime('%dth %B %Y').replace('0th', 'th')})"
                for ticket in tickets
            ])
    except Exception as e:
        return f"Error fetching guest requests: {str(e)}"

@tool
def close_guest_request(ticket_id: int = None):
    """
    Close a guest request ticket by ID.

    Args:
        ticket_id (int): Ticket number to be closed.

    Returns:
        str: Confirmation message.
    """
    if not ticket_id:
        return missing_param_response("ticket_id")
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id
                FROM public.dh_tickets
                WHERE id = %s;
            """, (ticket_id,))
            if not cursor.fetchone():
                return f"No ticket found with ID {ticket_id}."
            cursor.execute("""
                UPDATE public.dh_tickets
                SET status = 'closed'
                WHERE id = %s;
            """, (ticket_id,))
            conn.commit()
            return f"Guest request ticket #{ticket_id} has been successfully closed."
    except Exception as e:
        conn.rollback()
        return f"Error closing guest request #{ticket_id}: {str(e)}"

@tool
def get_room_details(room_number: str = None):
    """
    Get details of a specific room.

    Args:
        room_number (str): Room number to query.

    Returns:
        str: Room details including type and status.
    """
    if not room_number:
        return missing_param_response("room_number")
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT room_number, room_type, status
                FROM public.dh_rooms
                WHERE room_number = %s;
            """, (room_number,))
            room = cursor.fetchone()
            if room:
                return f"Room {room[0]}: Type - {room[1]}, Status - {room[2]}"
            return f"No details found for room {room_number}."
    except Exception as e:
        return f"Error fetching details for room {room_number}: {str(e)}"

@tool
def get_all_guests():
    """
    Get a list of all guests from booking records.

    Returns:
        str: Guest names.
    """
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT DISTINCT guest_name
                FROM public.dh_bookings;
            """)
            guests = cursor.fetchall()
            if not guests:
                return "No guest data found."
            guests = [row[0] for row in guests]
            return "Guests:\n" + ", ".join(guests)
    except Exception as e:
        return f"Error fetching guests: {str(e)}"

@tool
def get_revenue_by_date(date: str = None):
    """
    Fetch revenue data for a specific date from bookings.

    Args:
        date (str): The date in any recognizable format.

    Returns:
        str: Total revenue amount.
    """
    if not date:
        return missing_param_response("date")
    parsed_date = parse_date(date)
    if not parsed_date:
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or 18th August."
    
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT total_amount
                FROM public.dh_bookings
                WHERE booking_date = %s;
            """, (parsed_date.strftime("%Y-%m-%d"),))
            rows = cursor.fetchall()
            if not rows:
                return f"No revenue found on {parsed_date.strftime('%dth %B %Y').replace('0th', 'th')}."
            total = sum(float(row[0]) for row in rows)
            return f"Total revenue on {parsed_date.strftime('%dth %B %Y').replace('0th', 'th')}: ${total:.2f}"
    except Exception as e:
        return f"Error fetching revenue for {parsed_date.strftime('%dth %B %Y').replace('0th', 'th')}: {str(e)}"

@tool
def get_occupancy_rate(date: str = None):
    """
    Calculate the occupancy rate for a specific date.

    Args:
        date (str): The date in any recognizable format.

    Returns:
        str: Occupancy rate percentage.
    """
    if not date:
        return missing_param_response("date")
    parsed_date = parse_date(date)
    if not parsed_date:
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or 18th August."
    
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) FROM public.dh_rooms;
            """)
            total_rooms = cursor.fetchone()[0]
            cursor.execute("""
                SELECT COUNT(*)
                FROM public.dh_bookings
                WHERE %s BETWEEN check_in_date AND check_out_date;
            """, (parsed_date.strftime("%Y-%m-%d"),))
            occupied_rooms = cursor.fetchone()[0]
            if total_rooms == 0:
                return "No rooms registered in the system."
            occupancy_rate = (occupied_rooms / total_rooms) * 100
            return f"Occupancy rate on {parsed_date.strftime('%dth %B %Y').replace('0th', 'th')}: {occupancy_rate:.2f}%"
    except Exception as e:
        return f"Error calculating occupancy rate for {parsed_date.strftime('%dth %B %Y').replace('0th', 'th')}: {str(e)}"

@tool
def get_top_booking_source():
    """
    Identify the booking source with the highest total revenue.

    Returns:
        str: Booking source and revenue amount.
    """
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT booking_source, SUM(total_amount)
                FROM public.dh_bookings
                GROUP BY booking_source;
            """)
            sources = cursor.fetchall()
            if not sources:
                return "No booking data found."
            top_source = max(sources, key=lambda x: x[1])
            return f"The top booking source is {top_source[0]} with ${top_source[1]:.2f} in revenue."
    except Exception as e:
        return f"Error fetching top booking source: {str(e)}"

@tool
def list_room_types():
    """
    List all room types and their rates per night.

    Returns:
        str: List of room types with their rates.
    """
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT room_type, rate_per_night
                FROM public.dh_roomtypes
                ORDER BY room_type;
            """)
            room_types = cursor.fetchall()
            if not room_types:
                return "No room types found in the system."
            return "\n".join([f"{row[0]}: ${row[1]:.2f} per night" for row in room_types])
    except Exception as e:
        return f"Error fetching room types: {str(e)}"
    
@tool
def list_booking_sources():
    """
    List all booking sources.

    Returns:
        str: List of booking sources.
    """
    try:
        conn = get_db_connection_safe()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT booking_source
                FROM public.dh_bookings;
            """)
            booking_source = cursor.fetchall()
            if not booking_source:
                return "No booking sources found in the system."
            return "\n".join([row[0] for row in booking_source])
    except Exception as e:
        return f"Error fetching booking sources: {str(e)}"


### AGENT CONFIGURATION ###

tools = [
    check_room_availability,
    book_room,
    raise_guest_request,
    view_guest_requests,
    close_guest_request,
    get_room_details,
    get_all_guests,
    get_revenue_by_date,
    get_occupancy_rate,
    get_top_booking_source,
    list_room_types,
    list_booking_sources
]

SYSTEM_PROMPT = """
You are a reliable assistant helping a motel manager manage bookings, guest services, and reporting. You respond naturally and map user requests to the appropriate tool below using only factual data—never fabricate.

Respond in plain text (no emojis or markdown), format all dates conversationally (e.g., 18-07-2025 → 18th July 2025, 22:00 → 10pm).

TOOLS AND WHEN TO USE THEM

ROOM BOOKING
- check_room_availability(check_in_date, check_out_date): When user asks about available rooms or date-based availability.
- book_room(guest_name, room_number, check_in_date, check_out_date, session_id): When user wants to book a room. Use guest_name from session if not provided.
- get_room_details(room_number): When user asks about a room’s status or details.

GUEST REQUESTS
- raise_guest_request(room_number, request_description): When a guest needs something (e.g., extra towels). Prompt for room_number if missing.
- view_guest_requests(): When user asks to list open guest requests.
- close_guest_request(ticket_id): When user wants to close a guest request. Prompt if ID is missing.

REPORTING
- get_all_guests(): When user asks who is staying or to list guests.
- get_revenue_by_date(date): When user asks about earnings on a specific date.
- get_occupancy_rate(date): When user asks how full the motel is on a date.
- get_top_booking_source(): When user asks which booking source generates most revenue.

ROOM TYPES
- list_room_types(): When user asks what kinds of rooms exist or their rates.

BOOKING SOURCES
- list_booking_sources() : When user asks how to book or what platforms or sources can be used to book a room.

MEMORY BEHAVIOR
- Store and reuse last mentioned: room_number, guest_name, check_in_date, check_out_date, last_action.
- Map pronouns like “it”, “that”, or “again” to the most recent context.
  - "Book it again" → last room_number
  - "Raise request for it" → last room_number
  - "Book another room" → use last guest_name

DATE INTERPRETATION
- If year is missing: assume current year if future date, next year if past.
- Prompt for clarification if ambiguous.

GENERAL BEHAVIOR
- Use only tool outputs.
- Keep responses concise, factual, and clear.
- Avoid unnecessary verbosity.
"""


# Initialize agent with persistent memory
agent_executor = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT, checkpointer=memory, debug=False)

def ask_agent(user_input: str, session_id: str) -> dict:
    """
    Send a user message to the agent and get its response.
    Handles 'hi' with a dynamic welcome message based on available tools.
    """
    config = {"configurable": {"thread_id": session_id}}
    if user_input.lower().strip() == "hi":
        tool_descriptions = {
            "check_room_availability": "check room availability for specific dates",
            "book_room": "book rooms for guests",
            "raise_guest_request": "raise guest requests like extra towels",
            "view_guest_requests": "view open guest requests",
            "close_guest_request": "close guest request tickets",
            "get_room_details": "get details about a specific room",
            "get_all_guests": "list all current guests",
            "get_revenue_by_date": "check revenue for a specific date",
            "get_occupancy_rate": "calculate occupancy rate for a date",
            "get_top_booking_source": "find the top booking source by revenue",
            "list_room_types": "list all room types and their rates"
        }
        capabilities = ", ".join([desc for _, desc in tool_descriptions.items()])
        welcome_message = (
            f"Hello! I'm here to help you manage the motel. I can {capabilities}. "
            "Just tell me what you need, like 'Check availability for next week' or 'List room types.' How can I help you today?"
        )
        return {"text": welcome_message, "video_url": None}
    
    try:
        response = agent_executor.invoke(
            {"messages": [{"role": "user", "content": user_input}], "session_id": session_id},
            config=config
        )
        ans = str(response['messages'][-1].content).replace("*", "")
        print(ans)
        return {"text": ans, "video_url": None}
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        return {"text": f"Something went wrong: {e}", "video_url": None}