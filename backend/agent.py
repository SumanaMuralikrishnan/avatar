import os
import logging
from datetime import datetime, date
from typing import Optional
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from dateutil import parser as date_parser
import psycopg2

# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

memory = MemorySaver()

llm = AzureChatOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    openai_api_version=os.getenv("AZURE_API_VERSION"),
    azure_deployment=os.getenv("AZURE_DEPLOYMENT_NAME"),
    openai_api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    temperature=0.5,
)

# ---------------------------------------------------------------------------
# DATABASE — resilient singleton connection
# ---------------------------------------------------------------------------

_connection: Optional[psycopg2.extensions.connection] = None


def get_db_connection() -> psycopg2.extensions.connection:
    """
    Return a live psycopg2 connection.
    Reconnects automatically if the connection is closed or dead.
    """
    global _connection
    try:
        # Check if connection exists and is still alive
        if _connection is None or _connection.closed:
            raise psycopg2.OperationalError("Connection is None or closed.")
        # Ping the server
        with _connection.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        # Reconnect
        logger.info("Re-establishing database connection...")
        connection_string = os.getenv("DATABASE_URL")
        if not connection_string:
            raise ValueError("DATABASE_URL environment variable is not set.")
        _connection = psycopg2.connect(connection_string)
        logger.info("Database connection established.")
    return _connection


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def missing_param_response(param: str) -> str:
    return f"{param.replace('_', ' ').capitalize()} is missing. Could you please provide it?"


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string flexibly, assuming current or next year if not specified."""
    try:
        parsed = date_parser.parse(date_str, dayfirst=False)
        current_year = datetime.now().year
        if parsed.year < current_year:
            parsed = parsed.replace(
                year=current_year if parsed.date() >= date.today() else current_year + 1
            )
        return parsed
    except ValueError:
        return None


def fmt_date(dt: datetime) -> str:
    """Format a datetime as e.g. '18th July 2025'."""
    day = dt.day
    suffix = (
        "th" if 11 <= day <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    )
    return dt.strftime(f"%-d{suffix} %B %Y")  # Linux; use %#d on Windows


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------

@tool
def check_room_availability(check_in_date: str = None, check_out_date: str = None) -> str:
    """
    Check room availability for a given date range.

    Args:
        check_in_date: Check-in date in any recognizable format (e.g. '2025-07-18', '18th July').
        check_out_date: Check-out date in any recognizable format.

    Returns:
        Available room numbers with room types, or an unavailability message.
    """
    if not check_in_date:
        return missing_param_response("check_in_date")
    if not check_out_date:
        return missing_param_response("check_out_date")

    check_in = parse_date(check_in_date)
    check_out = parse_date(check_out_date)

    if not check_in or not check_out:
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or '18th July'."
    if check_in.date() < date.today():
        return f"Check-in date {fmt_date(check_in)} is in the past. Please provide a future date."
    if check_out <= check_in:
        return "Check-out date must be after check-in date."

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT room_number, room_type
                FROM public.dh_rooms
                WHERE room_number NOT IN (
                    SELECT room_number
                    FROM public.dh_bookings
                    WHERE check_in_date < %s AND check_out_date > %s
                )
                ORDER BY room_number;
                """,
                (check_out.strftime("%Y-%m-%d"), check_in.strftime("%Y-%m-%d")),
            )
            rooms = cur.fetchall()

        if not rooms:
            return f"No rooms available between {fmt_date(check_in)} and {fmt_date(check_out)}."

        room_list = ", ".join(f"Room {r[0]} ({r[1]})" for r in rooms)
        return f"Available rooms from {fmt_date(check_in)} to {fmt_date(check_out)}: {room_list}"

    except Exception as e:
        logger.error("check_room_availability error: %s", e, exc_info=True)
        return f"Error checking room availability: {e}"


@tool
def book_room(
    guest_name: str = None,
    room_number: str = None,
    check_in_date: str = None,
    check_out_date: str = None,
    config: RunnableConfig = None,
) -> str:
    """
    Book a room for a guest, automatically calculating the total amount.

    Args:
        guest_name: Full name of the guest.
        room_number: Room number to book.
        check_in_date: Check-in date in any recognizable format.
        check_out_date: Check-out date in any recognizable format.

    Returns:
        Booking confirmation with dates and total cost.
    """
    # Retrieve guest_name from memory if not provided
    if not guest_name and config:
        session_id = config.get("configurable", {}).get("thread_id")
        if session_id:
            checkpoint = memory.get({"configurable": {"thread_id": session_id}})
            if checkpoint:
                guest_name = (
                    checkpoint.get("channel_values", {})
                    .get("user_config", {})
                    .get("customer_name")
                )

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
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or '18th July'."
    if check_in.date() < date.today():
        return f"Check-in date {fmt_date(check_in)} is in the past. Please provide a future date."
    if check_out <= check_in:
        return "Check-out date must be after check-in date."

    conn = None
    try:
        conn = get_db_connection()
        nights = (check_out - check_in).days

        with conn.cursor() as cur:
            # Get room type
            cur.execute(
                "SELECT room_type FROM public.dh_rooms WHERE room_number = %s;",
                (room_number,),
            )
            room = cur.fetchone()
            if not room:
                return f"No room found with number {room_number}."

            # Get rate
            cur.execute(
                "SELECT rate_per_night FROM public.dh_roomtypes WHERE room_type = %s;",
                (room[0],),
            )
            rate = cur.fetchone()
            if not rate:
                return f"No rate found for room type {room[0]}."

            total_amount = rate[0] * nights

            # Insert booking
            cur.execute(
                """
                INSERT INTO public.dh_bookings
                    (guest_name, room_number, check_in_date, check_out_date, total_amount, booking_date)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    guest_name,
                    room_number,
                    check_in.strftime("%Y-%m-%d"),
                    check_out.strftime("%Y-%m-%d"),
                    total_amount,
                    datetime.now(),
                ),
            )
            conn.commit()

        return (
            f"Room {room_number} booked for {guest_name} "
            f"from {fmt_date(check_in)} to {fmt_date(check_out)} "
            f"for ${total_amount:.2f}."
        )

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("book_room error: %s", e, exc_info=True)
        return f"Error booking room {room_number}: {e}"


@tool
def raise_guest_request(room_number: str = None, request_description: str = "Guest request") -> str:
    """
    Raise a guest request ticket for a room.

    Args:
        room_number: Room number raising the request.
        request_description: Description of what the guest needs.

    Returns:
        Confirmation message with ticket ID.
    """
    if not room_number:
        return missing_param_response("room_number")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.dh_tickets
                    (room_number, request_description, status, assigned_to_department, created_at)
                VALUES (%s, %s, 'open', 'housekeeping', %s)
                RETURNING id;
                """,
                (room_number, request_description, datetime.now()),
            )
            ticket_id = cur.fetchone()[0]
            conn.commit()

        return f"Guest request ticket #{ticket_id} raised for room {room_number}: {request_description}."

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("raise_guest_request error: %s", e, exc_info=True)
        return f"Error raising guest request for room {room_number}: {e}"


@tool
def view_guest_requests() -> str:
    """
    View all open guest request tickets.

    Returns:
        List of open ticket details.
    """
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, room_number, request_description, assigned_to_department, created_at
                FROM public.dh_tickets
                WHERE status = 'open'
                ORDER BY created_at DESC;
                """
            )
            tickets = cur.fetchall()

        if not tickets:
            return "No open guest requests found."

        return "\n".join(
            f"Ticket #{t[0]} - Room {t[1]}: {t[2]} "
            f"(Assigned to {t[3]}, Created {fmt_date(t[4])})"
            for t in tickets
        )

    except Exception as e:
        logger.error("view_guest_requests error: %s", e, exc_info=True)
        return f"Error fetching guest requests: {e}"


@tool
def close_guest_request(ticket_id: int = None) -> str:
    """
    Close a guest request ticket by ID.

    Args:
        ticket_id: The ticket number to close.

    Returns:
        Confirmation message.
    """
    if not ticket_id:
        return missing_param_response("ticket_id")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM public.dh_tickets WHERE id = %s;", (ticket_id,)
            )
            if not cur.fetchone():
                return f"No ticket found with ID {ticket_id}."

            cur.execute(
                "UPDATE public.dh_tickets SET status = 'closed' WHERE id = %s;",
                (ticket_id,),
            )
            conn.commit()

        return f"Guest request ticket #{ticket_id} has been successfully closed."

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("close_guest_request error: %s", e, exc_info=True)
        return f"Error closing guest request #{ticket_id}: {e}"


@tool
def get_room_details(room_number: str = None) -> str:
    """
    Get details of a specific room.

    Args:
        room_number: The room number to query.

    Returns:
        Room type and status.
    """
    if not room_number:
        return missing_param_response("room_number")

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT room_number, room_type, status FROM public.dh_rooms WHERE room_number = %s;",
                (room_number,),
            )
            room = cur.fetchone()

        if not room:
            return f"No details found for room {room_number}."
        return f"Room {room[0]}: Type - {room[1]}, Status - {room[2]}"

    except Exception as e:
        logger.error("get_room_details error: %s", e, exc_info=True)
        return f"Error fetching details for room {room_number}: {e}"


@tool
def get_all_guests() -> str:
    """
    Get a list of all guests from booking records.

    Returns:
        Distinct guest names.
    """
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT guest_name FROM public.dh_bookings ORDER BY guest_name;")
            guests = cur.fetchall()

        if not guests:
            return "No guest data found."
        return "Guests: " + ", ".join(row[0] for row in guests)

    except Exception as e:
        logger.error("get_all_guests error: %s", e, exc_info=True)
        return f"Error fetching guests: {e}"


@tool
def get_revenue_by_date(date: str = None) -> str:
    """
    Fetch total revenue for rooms occupied on a specific date.

    Args:
        date: The date in any recognizable format.

    Returns:
        Total revenue amount for that date.
    """
    if not date:
        return missing_param_response("date")

    parsed_date = parse_date(date)
    if not parsed_date:
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or '18th July'."

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Revenue = bookings where the guest was actually staying that night
            cur.execute(
                """
                SELECT total_amount, check_in_date, check_out_date
                FROM public.dh_bookings
                WHERE check_in_date <= %s AND check_out_date > %s;
                """,
                (parsed_date.strftime("%Y-%m-%d"), parsed_date.strftime("%Y-%m-%d")),
            )
            rows = cur.fetchall()

        if not rows:
            return f"No revenue found for {fmt_date(parsed_date)}."

        # Pro-rate each booking to a single night's contribution
        total = sum(
            float(row[0]) / max((row[2] - row[1]).days, 1)
            for row in rows
        )
        return f"Total revenue on {fmt_date(parsed_date)}: ${total:.2f}"

    except Exception as e:
        logger.error("get_revenue_by_date error: %s", e, exc_info=True)
        return f"Error fetching revenue for {fmt_date(parsed_date)}: {e}"


@tool
def get_occupancy_rate(date: str = None) -> str:
    """
    Calculate the occupancy rate for a specific date.

    Args:
        date: The date in any recognizable format.

    Returns:
        Occupancy rate as a percentage.
    """
    if not date:
        return missing_param_response("date")

    parsed_date = parse_date(date)
    if not parsed_date:
        return "Invalid date format. Please use a recognizable format like YYYY-MM-DD or '18th July'."

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.dh_rooms;")
            total_rooms = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.dh_bookings
                WHERE check_in_date <= %s AND check_out_date > %s;
                """,
                (parsed_date.strftime("%Y-%m-%d"), parsed_date.strftime("%Y-%m-%d")),
            )
            occupied_rooms = cur.fetchone()[0]

        if total_rooms == 0:
            return "No rooms registered in the system."

        rate = (occupied_rooms / total_rooms) * 100
        return f"Occupancy rate on {fmt_date(parsed_date)}: {rate:.2f}% ({occupied_rooms}/{total_rooms} rooms)"

    except Exception as e:
        logger.error("get_occupancy_rate error: %s", e, exc_info=True)
        return f"Error calculating occupancy rate: {e}"


@tool
def get_top_booking_source() -> str:
    """
    Identify the booking source with the highest total revenue.

    Returns:
        Top booking source and its revenue.
    """
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT booking_source, SUM(total_amount) AS total
                FROM public.dh_bookings
                GROUP BY booking_source
                ORDER BY total DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()

        if not row:
            return "No booking data found."
        return f"Top booking source: {row[0]} with ${row[1]:.2f} in total revenue."

    except Exception as e:
        logger.error("get_top_booking_source error: %s", e, exc_info=True)
        return f"Error fetching top booking source: {e}"


@tool
def list_room_types() -> str:
    """
    List all room types and their rates per night.

    Returns:
        Room types with nightly rates.
    """
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT room_type, rate_per_night FROM public.dh_roomtypes ORDER BY room_type;"
            )
            room_types = cur.fetchall()

        if not room_types:
            return "No room types found in the system."
        return "\n".join(f"{row[0]}: ${row[1]:.2f} per night" for row in room_types)

    except Exception as e:
        logger.error("list_room_types error: %s", e, exc_info=True)
        return f"Error fetching room types: {e}"


@tool
def list_booking_sources() -> str:
    """
    List all distinct booking sources used in existing bookings.

    Returns:
        Distinct booking sources.
    """
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT booking_source FROM public.dh_bookings ORDER BY booking_source;"
            )
            sources = cur.fetchall()

        if not sources:
            return "No booking sources found in the system."
        return "Booking sources: " + ", ".join(row[0] for row in sources)

    except Exception as e:
        logger.error("list_booking_sources error: %s", e, exc_info=True)
        return f"Error fetching booking sources: {e}"


# ---------------------------------------------------------------------------
# AGENT CONFIGURATION
# ---------------------------------------------------------------------------

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
    list_booking_sources,
]

def get_system_prompt() -> str:
    today = datetime.now().strftime("%A, %d %B %Y")  # e.g. "Sunday, 05 April 2026"
    return f"""
    You are a reliable assistant helping a hotel manager manage bookings, guest services, and reporting.
    You respond naturally and map user requests to the appropriate tool using only factual data — never fabricate.
    
    Respond in plain text (no emojis, no markdown). Format all dates conversationally (e.g. 18th July 2025).
    
    TOOLS AND WHEN TO USE THEM
    
    ROOM BOOKING
    - check_room_availability(check_in_date, check_out_date): User asks about available rooms for a date range.
    - book_room(guest_name, room_number, check_in_date, check_out_date): User wants to book a room.
    - get_room_details(room_number): User asks about a specific room's status or type.
    
    GUEST REQUESTS
    - raise_guest_request(room_number, request_description): Guest needs something (e.g. extra towels).
    - view_guest_requests(): User asks to see open requests.
    - close_guest_request(ticket_id): User wants to close a ticket.
    
    REPORTING
    - get_all_guests(): List all current guests.
    - get_revenue_by_date(date): Revenue for a specific date (rooms occupied that night).
    - get_occupancy_rate(date): How full the hotel is on a given date.
    - get_top_booking_source(): Which platform generates the most revenue.
    
    ROOM TYPES
    - list_room_types(): User asks what room categories exist or their rates.
    
    BOOKING SOURCES
    - list_booking_sources(): User asks what platforms or channels are used for bookings.
    
    MEMORY BEHAVIOR
    - Reuse the last mentioned room_number, guest_name, check_in_date, check_out_date across turns.
    - Map pronouns like "it", "that", or "again" to the most recent relevant context.
    
    DATE INTERPRETATION
    - If the year is missing, assume current year if the date is in the future, next year if it has already passed.
    - Ask for clarification if the date is genuinely ambiguous.
    
    GENERAL BEHAVIOR
    - Use only tool outputs. Never invent data.
    - Keep responses concise, factual, and clear.
    """

agent_executor = create_react_agent(
    llm,
    tools,
    prompt=SYSTEM_PROMPT,
    checkpointer=memory,
    debug=False,
)


# ---------------------------------------------------------------------------
# MESSAGE HISTORY SANITIZER
# ---------------------------------------------------------------------------

def sanitize_message_history(config: dict) -> None:
    """
    Remove any AIMessages with tool_calls that have no matching ToolMessage.
    This prevents INVALID_CHAT_HISTORY errors caused by interrupted tool executions.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    checkpoint = memory.get(config)
    if not checkpoint:
        return

    messages = checkpoint.get("channel_values", {}).get("messages", [])
    if not messages:
        return

    responded_ids = {
        msg.tool_call_id
        for msg in messages
        if isinstance(msg, ToolMessage)
    }

    cleaned = []
    dropped = False
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            unmatched = [tc for tc in msg.tool_calls if tc["id"] not in responded_ids]
            if unmatched:
                logger.warning(
                    "Dropping AIMessage with unmatched tool calls: %s",
                    [tc["name"] for tc in unmatched],
                )
                dropped = True
                continue
        cleaned.append(msg)

    if dropped:
        checkpoint["channel_values"]["messages"] = cleaned
        memory.put(config, checkpoint, {})


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------

WELCOME_MESSAGE = (
    "Hello! I am here to help you manage the hotel. I can check room availability, "
    "book rooms for guests, raise and close guest requests, list room types and rates, "
    "and pull reports on revenue, occupancy, guests, and booking sources. "
    "Just tell me what you need. How can I help you today?"
)


def ask_agent(user_input: str, session_id: str) -> dict:
    """
    Send a user message to the agent and return its response.
    """
    if user_input.lower().strip() in {"hi", "hello", "hey"}:
        return {"text": WELCOME_MESSAGE, "video_url": None}

    config = {"configurable": {"thread_id": session_id}}

    # Sanitize history before invoking to prevent INVALID_CHAT_HISTORY
    try:
        sanitize_message_history(config)
    except Exception as e:
        logger.warning("Could not sanitize message history: %s", e)

    try:
        response = agent_executor.invoke(
            {
                "messages": [{"role": "user", "content": user_input}],
                "system": get_system_prompt(),   # ✅ fresh date every call
            },
            config=config,
        )
        ans = str(response["messages"][-1].content).replace("*", "")
        return {"text": ans, "video_url": None}

    except Exception as e:
        logger.error("Agent error: %s", e, exc_info=True)
        return {"text": f"Something went wrong: {e}", "video_url": None}
###############
# import os
# import logging
# from pathlib import Path
 
# from dotenv import load_dotenv
# from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
# from langchain_core.tools import tool
# from langgraph.checkpoint.memory import MemorySaver
# from langgraph.prebuilt import create_react_agent
 
# import chromadb
# from langchain_text_splitters import RecursiveCharacterTextSplitter
# from langchain_community.document_loaders import Docx2txtLoader
 
 
# # ---------------------------------------------------------
# # SETUP
# # ---------------------------------------------------------
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)
 
# load_dotenv()
# memory = MemorySaver()
 
 
# # ---------------------------------------------------------
# # LLM (Azure OpenAI)
# # ---------------------------------------------------------
# llm = AzureChatOpenAI(
#     azure_endpoint=os.getenv("azure_base_url"),
#     openai_api_version=os.getenv("azure_api_version"),
#     azure_deployment="gpt-4o",
#     openai_api_key=os.getenv("azure_api_key"),
#     temperature=0.5
# )
 
 
# # ---------------------------------------------------------
# # RAG SETUP (Chroma + Embeddings)
# # ---------------------------------------------------------
 
# # Embeddings
# embedding_fn = AzureOpenAIEmbeddings(
#     model=os.getenv("Embedding_model"),
#     azure_endpoint=os.getenv("azure_base_url"),
#     openai_api_key=os.getenv("azure_api_key"),
#     deployment=os.getenv("Embedding_model"),
#     openai_api_version=os.getenv("azure_api_version")
# )
 
# # ChromaDB Cloud Client
# chroma_client = chromadb.CloudClient(
#     api_key=os.getenv("CHROMA_API_KEY"),
#     tenant=os.getenv("CHROMA_TENANT"),
#     database=os.getenv("CHROMA_DB")
# )
 
# collection = chroma_client.get_or_create_collection("documents")
 
 
# # Folder containing SOP files (.docx)
# # DOC_FOLDER = Path(os.getenv("DOC_FOLDER", r"backend\data"))
# from pathlib import Path

# BASE_DIR = Path(__file__).resolve().parent      # directory containing this file
# DOC_FOLDER = BASE_DIR / "data"   
 
# text_splitter = RecursiveCharacterTextSplitter(
#     chunk_size=500,
#     chunk_overlap=50
# )
 
 
# def load_documents_into_chroma():
#     """
#     Run manually ONCE to ingest documents into Chroma.
#     """
#     for file in DOC_FOLDER.glob("*.docx"):
#         loader = Docx2txtLoader(str(file))
#         docs = loader.load()
 
#         text = " ".join([d.page_content for d in docs])
#         chunks = text_splitter.split_text(text)
#         embeddings = embedding_fn.embed_documents(chunks)
 
#         collection.add(
#             documents=chunks,
#             metadatas=[{"source": str(file)}] * len(chunks),
#             ids=[f"{file.stem}_{i}" for i in range(len(chunks))],
#             embeddings=embeddings
#         )
 
#     print("SOP documents loaded.")
 
 
# # ---------------------------------------------------------
# # RAG QUERY TOOL
# # ---------------------------------------------------------
 
# @tool
# def rag_query(question: str):
#     """
#     Retrieve an answer from SOP files using Chroma RAG.
#     """
#     if not question:
#         return "Please provide a question."
 
#     query_emb = embedding_fn.embed_query(question)
#     results = collection.query(query_embeddings=query_emb, n_results=3)
 
#     if not results["documents"]:
#         return "No matching information found in SOP."
 
#     context = "\n\n".join(results["documents"][0])
 
#     prompt = f"""
#     Use ONLY the following context to answer:
 
#     CONTEXT:
#     {context}
 
#     QUESTION:
#     {question}
 
#     ANSWER:
#     """
 
#     response = llm.invoke(prompt)
#     return response.content
 
 
# # ---------------------------------------------------------
# # AGENT
# # ---------------------------------------------------------
 
# tools = [rag_query]
 
# SYSTEM_PROMPT = """
# You are an assistant for retrieving SOP and procedural information.
# Whenever the user asks a question, call the rag_query tool.
# Do not guess. Use only the RAG tool output.
# The respons must not contain any markdown characters like "*", "#" or any emoji's.
# """
 
# agent_executor = create_react_agent(
#     llm,                     # model
#     tools,                   # tools list
#     prompt=SYSTEM_PROMPT,    # system prompt
#     checkpointer=memory      # conversation memory
# )
 
 
# # ---------------------------------------------------------
# # ENTRYPOINT FOR FASTAPI
# # ---------------------------------------------------------
 
# def ask_agent(user_input: str, session_id: str) -> dict:
#     """
#     Main function called by FastAPI (app.py).
#     """
#     config = {"configurable": {"thread_id": session_id}}
 
#     try:
#         response = agent_executor.invoke(
#             {
#                 "messages": [{"role": "user", "content": user_input}],
#                 "session_id": session_id
#             },
#             config=config
#         )
 
#         output = response["messages"][-1].content
#         return {
#             "text": output,
#             "video_url": None
#         }
 
#     except Exception as e:
#         logger.error(f"Agent error: {e}")
#         return {
#             "text": f"Error: {e}",
#             "video_url": None
#         }
 
