import os, json, base64, re, logging
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import httpx
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return val

ANTHROPIC_API_KEY = _require_env("ANTHROPIC_API_KEY")
IMMICH_API_KEY = _require_env("IMMICH_API_KEY")
IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich_server:2283")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", "")

VALID_TOPICS = {
    "Respiratory", "Cardiology", "Neurology", "Gastroenterology", "Endocrinology",
    "Renal", "Immunology", "Pharmacology", "Anatomy", "Physiology",
    "Biochemistry", "Histology", "Genetics", "MSK", "Psychiatry", "General",
}

PROMPT = """You are a medical education expert. Analyse this whiteboard or lecture image.

First identify the medical topic. Use one of: Respiratory, Cardiology, Neurology, Gastroenterology, Endocrinology, Renal, Immunology, Pharmacology, Anatomy, Physiology, Biochemistry, Histology, Genetics, MSK, Psychiatry, or General.

Then generate 4-10 Anki flashcards. Use a mix of:
- "basic" cards: a question on the front, answer on the back
- "cloze" cards: a sentence with key terms replaced by {{c1::term}}, {{c2::term}} etc.

Use cloze for definitions, lists, and mechanisms. Use basic for concepts requiring explanation.

Return ONLY valid JSON with no other text:
{
  "topic": "Respiratory",
  "cards": [
    {"type": "basic", "front": "What is the role of surfactant?", "back": "Reduces alveolar surface tension, preventing collapse"},
    {"type": "cloze", "text": "Surfactant is produced by {{c1::type II pneumocytes}} and contains {{c2::dipalmitoylphosphatidylcholine (DPPC)}}"}
  ]
}"""

_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)

async def _verify_api_key(api_key: str = Depends(_api_key_header)):
    if SERVICE_API_KEY and api_key != SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

class ProcessRequest(BaseModel):
    assetId: str

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/process")
async def process(req: ProcessRequest, _=Depends(_verify_api_key)):
    log.info("Processing asset %s", req.assetId)

    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            f"{IMMICH_URL}/api/assets/{req.assetId}/thumbnail?size=preview",
            headers={"x-api-key": IMMICH_API_KEY},
        )
        if r.status_code != 200:
            log.error("Immich returned %d for asset %s", r.status_code, req.assetId)
            raise HTTPException(500, f"Immich returned {r.status_code}")
        image_b64 = base64.b64encode(r.content).decode()

    log.info("Sending asset %s to Claude (%s)", req.assetId, CLAUDE_MODEL)
    try:
        ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = ai.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )
    except anthropic.APIError as e:
        log.error("Claude API error for asset %s: %s", req.assetId, e)
        raise HTTPException(502, f"Claude API error: {e}")

    if not message.content:
        log.error("Empty Claude response for asset %s", req.assetId)
        raise HTTPException(500, "Empty response from Claude")

    text = message.content[0].text
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        log.error("No JSON in Claude response for asset %s: %.200s", req.assetId, text)
        raise HTTPException(500, f"No JSON in Claude response: {text[:200]}")

    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError as e:
        log.error("JSON parse error for asset %s: %s", req.assetId, e)
        raise HTTPException(500, f"Invalid JSON in Claude response: {e}")

    topic = parsed.get("topic", "General")
    if topic not in VALID_TOPICS:
        log.warning("Claude returned unknown topic %r for asset %s, defaulting to General", topic, req.assetId)
        topic = "General"

    cards = parsed.get("cards", [])
    log.info("Asset %s -> topic=%s, %d cards generated", req.assetId, topic, len(cards))
    return {"cards": cards, "topic": topic}
