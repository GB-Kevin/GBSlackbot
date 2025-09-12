from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import google.generativeai as genai
import os
import requests
from flask import Flask
import threading
import logging
from typing import Dict, Optional

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config for "long response" UX ---
# If the bot hasn't finished responding within these delays, it will:
#  - send an ephemeral "Working on it…" to the user
#  - post a public "thinking…" placeholder in a thread
EPHEMERAL_DELAY_SEC = 0.5
PLACEHOLDER_DELAY_SEC = 1.0

# --- Slack Setup ---
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# --- Gemini Setup ---
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-1.5-flash-latest")

# --- GitHub Docs Setup ---
OWNER = "GB-Kevin"
REPO = "GBSlackbot"
BRANCH = "main"
FOLDER = "docs"

def load_docs_from_github(owner, repo, branch, folder):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{folder}?ref={branch}"
    resp = requests.get(url)
    resp.raise_for_status()
    files = resp.json()
    docs = {}
    for file in files:
        if file["name"].endswith(".txt"):
            raw_url = file["download_url"]
            text = requests.get(raw_url).text
            docs[file["name"]] = text
    logger.info(f"Loaded {len(docs)} .txt files from GitHub")
    return docs

docs = load_docs_from_github(OWNER, REPO, BRANCH, FOLDER)
personality = docs.get("personality.txt", """
Tone: Neutral and helpful.
Keep answers concise.
Do not use jokes unless asked.
""")

def extract_subject(query: str) -> str:
    subject_prompt = f"""
    Extract the main subject of this question in 1-3 words only.

    Question: {query}
    """
    resp = model.generate_content(subject_prompt)
    return (resp.text or "").strip() or "that topic"

def ask(query):
    file_list = "\n".join([f"- {name}" for name in docs.keys() if name != "personality.txt"])
    selector_prompt = f"""
    We have multiple subject documents:

    {file_list}

    Question: {query}

    Which file(s) are most relevant? 
    Reply with a comma-separated list of filenames, or "none" if nothing is relevant.
    """
    sel = model.generate_content(selector_prompt)
    chosen_text = (sel.text or "").strip().lower()
    logger.info(f"File selection response: {chosen_text}")

    if "none" in chosen_text or not chosen_text:
        subject = extract_subject(query)
        return f"I can't find anything about {subject}. Try asking @tech"

    chosen_files = [f.strip() for f in chosen_text.split(",") if f.strip() in docs]
    if not chosen_files:
        subject = extract_subject(query)
        return f"I can't find anything about {subject}. Try asking @tech"

    combined_context = ""
    max_chars = 12000
    for fname in chosen_files:
        chunk = docs[fname]
        if len(combined_context) + len(chunk) > max_chars:
            chunk = chunk[: max_chars - len(combined_context)]
        combined_context += f"\n--- {fname} ---\n{chunk}\n"
        if len(combined_context) >= max_chars:
            break

    final_prompt = f"""
    Personality guidelines:
    {personality}

    Context from docs:
    {combined_context}

    Question: {query}

    Please answer using the tone and humor guidelines above.
    If the context does not contain the answer, reply with:
    "I can't find anything about {extract_subject(query)}. Try asking @tech"
    """
    resp = model.generate_content(final_prompt)
    logger.info(f"Gemini response generated for query: {query}")
    return (resp.text or "").strip()

# --- Slack Event Handling with "long-running" UX ---
@app.event("app_mention")
def handle_mention(body, say, client, logger):
    evt: Dict = body.get("event", {})
    user: Optional[str] = evt.get("user")
    text: str = evt.get("text", "") or ""
    channel: Optional[str] = evt.get("channel")
    thread_ts: Optional[str] = evt.get("ts")  # reply in a thread to keep the channel tidy

    logger.info(f"Received mention from user {user} in {channel}: {text!r}")

    # Shared state for timers
    done = {"flag": False}
    placeholder = {"ts": None}

    # Timer: send ephemeral "working on it…" (only if still not done)
    def send_ephemeral():
        if not done["flag"] and channel and user:
            try:
                client.chat_postEphemeral(
                    channel=channel,
                    user=user,
                    text="⏳ Working on it…"
                )
                logger.info("Sent ephemeral 'working on it…'")
            except Exception as e:
                logger.warning(f"Failed to send ephemeral: {e}")

    # Timer: post public placeholder "thinking…" (threaded)
    def send_placeholder():
        if not done["flag"] and channel and user:
            try:
                res = say(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"🤖 <@{user}> thinking…"
                )
                placeholder["ts"] = res["ts"]
                logger.info(f"Posted placeholder message ts={placeholder['ts']}")
            except Exception as e:
                logger.warning(f"Failed to post placeholder: {e}")

    # Start timers; they will fire only if the work isn't done quickly
    threading.Timer(EPHEMERAL_DELAY_SEC, send_ephemeral).start()
    threading.Timer(PLACEHOLDER_DELAY_SEC, send_placeholder).start()

    # Do the actual work
    try:
        answer = ask(text)
        final_text = f"<@{user}> {answer}"
    except Exception as e:
        logger.exception("Error while generating answer")
        final_text = f"😬 <@{user}> I hit an error processing that."

    # Mark as done so timers (if not fired yet) won't send progress messages
    done["flag"] = True

    # If a placeholder exists, update it. Otherwise, just post the final message.
    try:
        if placeholder["ts"]:
            client.chat_update(channel=channel, ts=placeholder["ts"], text=final_text)
            logger.info("Updated placeholder with final answer.")
        else:
            # Post final message in the same thread as the mention
            say(channel=channel, thread_ts=thread_ts, text=final_text)
            logger.info("Posted final answer without placeholder.")
    except Exception as e:
        logger.warning(f"Failed to deliver final message/update: {e}")
        # Last attempt: try a plain post
        try:
            say(channel=channel, thread_ts=thread_ts, text=final_text)
        except Exception:
            logger.exception("Final fallback post failed.")

# --- Flask Keepalive Server (for status page) ---
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    # host=0.0.0.0 to bind externally on Render Web Services
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    logger.info("Starting Flask keepalive thread...")
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Starting Slack SocketModeHandler...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
