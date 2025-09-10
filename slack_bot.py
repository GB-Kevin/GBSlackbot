from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import google.generativeai as genai
import os
import requests
from flask import Flask
import threading

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
    return resp.text.strip()

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
    chosen_text = sel.text.strip().lower()

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
    return resp.text.strip()

# --- Slack Event Handling ---
@app.event("app_mention")
def handle_mention(body, say):
    user = body["event"]["user"]
    text = body["event"]["text"]
    answer = ask(text)
    say(f"<@{user}> {answer}")

# --- Flask Keepalive Server ---
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))  # Render sets PORT env
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Run Flask server in a background thread
    threading.Thread(target=run_flask, daemon=True).start()
    # Run Slack bot (blocking)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
