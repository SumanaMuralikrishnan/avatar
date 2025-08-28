from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
import logging

# Assuming ask_agent is defined in agent.py
from backend.agent import ask_agent

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Allow frontend JS to call backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    session_id: str
    message: str
    user: Optional[Dict] = None  # Make user optional

@app.get("/")
def read_root():
    return {"message": "FastAPI is live!"}

@app.post("/ask_agent")
def chat_with_agent(req: ChatRequest):
    try:
        
        return ask_agent(req.message, req.session_id)
        
    except Exception as e:
        return(e)
