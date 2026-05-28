import streamlit as st
import httpx
import uuid

import json
import os
from typing import Any, Dict, Final, Generator

WEBCHAT_STREAM_URL: Final = "http://localhost:8000/webchat/stream"
HEARTBEAT_POLL_URL: Final = "http://localhost:8000/webchat/heartbeat/poll"
TITLE: Final = "👩🏻 Selmakit Agent Dashboard"

# -- Configuration
st.set_page_config(
    page_title=TITLE, 
    layout="wide",
    initial_sidebar_state='expanded'
)

if 'config_editing' not in st.session_state:
        st.session_state.config_editing = False
        
# -- Custom CSS for fixed sidebar width
st.markdown("""
    <style>
    [data-testid="stSidebar"] {
        min-width: 200px;
        max-width: 200px;
    }
    </style>
""", unsafe_allow_html=True)

if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())[:8]

if "messages" not in st.session_state:
    st.session_state.messages = []

def parse_sse_events(response: httpx.Response) -> Generator[dict, None, None]:
    """Reads SSE lines, yields all events as dicts."""
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        yield event
        if event.get("type") in ("done", "error"):
            break

# -- Settings Dialog
CONFIG_FILE = ".selmakit/selmakit.json"

def read_raw_file(filepath: str) -> str:
    """
    Reads the file as raw text. 
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}") 
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content: str = f.read()
        return content


def write_raw_file(filepath: str, content: str) -> None:
    """Writes the provided raw string content to a file."""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


@st.dialog("⚙️ Settings", width="large")
def settings_dialog():
    st.subheader("Configuration")

    if 'config_raw_content' not in st.session_state or not st.session_state.config_editing:
        st.session_state.config_raw_content = read_raw_file(CONFIG_FILE)

    if not st.session_state.config_editing:

        col_a, col_b = st.columns([0.8, 0.2])

        with col_a:
            st.info(f"Currently viewing: `{CONFIG_FILE}`")
        with col_b:
            if st.button("✎ Edit File"):
                st.session_state.config_editing = True
                st.rerun()

        try:
            # Parse the raw string into a typed Dictionary for the st.json viewer
            json.loads(st.session_state.config_raw_content)
            st.json(json.loads(st.session_state.config_raw_content))
        except Exception as e:
            st.error(f"Error details: {e.msg} at line {e.lineno}, column {e.colno}")
            st.code(st.session_state.config_raw_content)

    else:
        # --- EDIT MODE ---
        st.subheader("Edit Mode")
        st.caption("Editing raw text. No comments allowed in standard JSON.")
        
        # edited_text will be a string from the text_area
        edited_text: str = st.text_area(
            label="JSON Content", 
            value=st.session_state.config_raw_content, 
            height=400
        )
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("💾 Save Changes"):
                try:
                    # Validation: Try to parse the input string to ensure it's valid JSON
                    _: Dict[str, Any] = json.loads(edited_text) 
                    
                    # Write the raw string to the file
                    write_raw_file(CONFIG_FILE, edited_text)
                    
                    st.session_state.config_raw_content = edited_text
                    st.session_state.config_editing = False

                    st.success("File updated successfully.")
                    st.rerun()

                except json.JSONDecodeError as e:
                    st.error(f"Validation Failed: {e.msg} at line {e.lineno}")
                    
        with col2:
            if st.button("✖ Discard"):
                st.session_state.config_editing = False
                st.rerun()

# -- Sidebar
st.sidebar.header(TITLE)
st.sidebar.image("images/selma.png", width=200)
if st.sidebar.button("⚙️ Settings"):
    settings_dialog()

if st.session_state.config_editing:
    settings_dialog()

# -- Alert polling (runs every 5 s independently of user input)

@st.fragment(run_every="5s")
def poll_alerts():
    try:
        resp = httpx.get(HEARTBEAT_POLL_URL, timeout=2.0)
        if resp.status_code == 200:
            alert = resp.json().get("alert")
            if alert:
                if isinstance(alert, dict) and alert.get("kind") in ("cron", "heartbeat"):
                    st.session_state.messages.append({"role": "cron", "content": alert["prompt"]})
                    if alert.get("reply"):
                        st.session_state.messages.append({"role": "assistant", "content": alert["reply"]})
                else:
                    text = alert.get("reply", str(alert)) if isinstance(alert, dict) else str(alert)
                    st.session_state.messages.append({"role": "notification", "content": text})
                st.rerun()
    except Exception:
        pass

poll_alerts()

# -- Chat

for message in st.session_state.messages:
    if message["role"] == "notification":
        st.warning(f"🔔 {message['content']}")
    elif message["role"] == "cron":
        st.info(f"⏰ {message['content']}")
    else:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

if prompt := st.chat_input("How can I help you today?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            payload = {
                "user_id": st.session_state.user_id,
                "text": prompt,
                "user_name": "Admin"
            }
            tool_status = st.empty()
            reply_box = st.empty()
            full_reply = ""

            with httpx.Client() as client:
                with client.stream("POST", WEBCHAT_STREAM_URL, json=payload, timeout=120.0) as response:
                    for event in parse_sse_events(response):
                        match event.get("type"):
                            case "tool":
                                tool_status.caption(f"🔧 {event.get('name', 'tool')}…")
                            case "chunk":
                                tool_status.empty()
                                full_reply += event.get("text", "")
                                reply_box.markdown(full_reply + "▌")
                            case "error":
                                tool_status.empty()
                                raise RuntimeError(event.get("message", "Unknown error."))
                            case "done":
                                tool_status.empty()
                                reply_box.markdown(full_reply)

            st.session_state.messages.append({"role": "assistant", "content": full_reply})
        except httpx.ConnectError:
            st.error("❌ Gateway unreachable. Is `gateway.py` running?")
        except RuntimeError as e:
            st.error(f"❌ {e}")
        except Exception as e:
            st.error(f"An error occurred: {e}")

