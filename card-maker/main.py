import os, json, base64, re
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
import anthropic

app = FastAPI()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich_server:2283")

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

class ProcessRequest(BaseModel):
    assetId: str

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/process")
async def process(req: ProcessRequest):
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            f"{IMMICH_URL}/api/assets/{req.assetId}/thumbnail?size=preview",
            headers={"x-api-key": IMMICH_API_KEY}
        )
        if r.status_code != 200:
            raise HTTPException(500, f"Immich returned {r.status_code}")
        image_b64 = base64.b64encode(r.content).decode()

    ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = ai.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": PROMPT}
            ]
        }]
    )

    text = message.content[0].text
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise HTTPException(500, f"No JSON in Claude response: {text[:200]}")
    parsed = json.loads(match.group())
    return {
        "cards": parsed["cards"],
        "topic": parsed.get("topic", "General")
    }
