import os, re, sqlite3, threading, time, logging
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Optional
import docker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

DB = os.environ.get("ANKI_DB_PATH", "/anki-data/collection.anki2")
ANKI_SYNC_CONTAINER = os.environ.get("ANKI_SYNC_CONTAINER", "anki-sync")
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", "")

# Prevents concurrent SQLite writes and container stop/start races
_write_lock = threading.Lock()

VALID_TOPICS = {
    "Respiratory", "Cardiology", "Neurology", "Gastroenterology", "Endocrinology",
    "Renal", "Immunology", "Pharmacology", "Anatomy", "Physiology",
    "Biochemistry", "Histology", "Genetics", "MSK", "Psychiatry", "General",
}

_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)

async def _verify_api_key(api_key: str = Depends(_api_key_header)):
    if SERVICE_API_KEY and api_key != SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

class Card(BaseModel):
    type: str = "basic"
    front: Optional[str] = None
    back: Optional[str] = None
    text: Optional[str] = None

class CardBatch(BaseModel):
    cards: list[Card]
    topic: str = "General"

def unicase(a, b):
    return (a.lower() > b.lower()) - (a.lower() < b.lower())

def get_or_create_deck(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM decks WHERE name=?", (name,)).fetchone()
    if row:
        return row[0]
    now = int(time.time())
    id_ = now * 1000 + int.from_bytes(os.urandom(3), "big")
    conn.execute(
        "INSERT INTO decks (id,name,mtime_secs,usn,common,kind) VALUES (?,?,?,?,?,?)",
        (id_, name, now, -1, bytes([0x08, 0x01, 0x10, 0x01]), bytes([0x0A, 0x02, 0x08, 0x01])),
    )
    return id_

def get_note_type_ids(conn):
    rows = conn.execute("SELECT id, name FROM notetypes").fetchall()
    if not rows:
        raise HTTPException(500, "No note types in collection")
    basic_id = None
    cloze_id = None
    for id_, name in rows:
        nl = name.lower()
        if "cloze" in nl:
            cloze_id = id_
        elif basic_id is None:
            basic_id = id_
    if basic_id is None:
        basic_id = rows[0][0]
    if cloze_id is None:
        cloze_id = basic_id
    return basic_id, cloze_id

def unique_id(conn, table: str) -> int:
    # table is always the literal string "notes" or "cards" — never user input
    for _ in range(20):
        id_ = int(time.time() * 1000) + int.from_bytes(os.urandom(2), "big") % 9999
        if not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone():
            return id_
        time.sleep(0.001)
    raise HTTPException(500, "Could not generate unique ID")

@app.post("/cards")
def create_cards(batch: CardBatch, _=Depends(_verify_api_key)):
    topic = batch.topic if batch.topic in VALID_TOPICS else "General"
    if topic != batch.topic:
        log.warning("Unknown topic %r, using General", batch.topic)

    if not _write_lock.acquire(timeout=30):
        raise HTTPException(503, "Server busy — try again shortly")

    log.info("Writing %d cards to deck autogen::%s", len(batch.cards), topic)
    dclient = docker.from_env()
    sync = dclient.containers.get(ANKI_SYNC_CONTAINER)
    sync.stop(timeout=5)
    time.sleep(0.5)

    try:
        conn = sqlite3.connect(DB, timeout=10)
        conn.create_collation("unicase", unicase)
        try:
            basic_ntid, cloze_ntid = get_note_type_ids(conn)
            get_or_create_deck(conn, "autogen")
            deck_id = get_or_create_deck(conn, f"autogen::{topic}")

            created = []
            for card in batch.cards:
                is_cloze = card.type == "cloze"
                ntid = cloze_ntid if is_cloze else basic_ntid

                if is_cloze:
                    if not card.text:
                        log.warning("Skipping cloze card with no text field")
                        continue
                    sfld = re.sub(r'\{\{c\d+::(.*?)\}\}', r'\1', card.text)
                    fields = f"{card.text}\x1f"
                else:
                    if not card.front or not card.back:
                        log.warning("Skipping basic card with missing front/back")
                        continue
                    sfld = card.front
                    fields = f"{card.front}\x1f{card.back}"

                note_id = unique_id(conn, "notes")
                card_id = unique_id(conn, "cards")
                now_s = int(time.time())
                csum = int.from_bytes(sfld.encode()[:4].ljust(4, b'\x00'), "big") % (2**31)

                conn.execute(
                    "INSERT INTO notes "
                    "(id,guid,mid,mod,usn,tags,flds,sfld,csum,flags,data) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (note_id, f"{note_id:x}", ntid, now_s, -1,
                     "autogen whiteboard ", fields, sfld, csum, 0, ""),
                )
                conn.execute(
                    "INSERT INTO cards "
                    "(id,nid,did,ord,mod,usn,type,queue,due,ivl,factor,"
                    "reps,lapses,left,odue,odid,flags,data) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (card_id, note_id, deck_id, 0, now_s, -1,
                     0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, ""),
                )
                created.append({"type": card.type, "content": sfld[:60]})

            conn.execute("UPDATE col SET mod=?", (int(time.time()),))
            conn.commit()
            log.info("Created %d cards in deck autogen::%s", len(created), topic)
            return {
                "created": len(created),
                "deck": f"autogen::{topic}",
                "cards": created,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        try:
            sync.start()
            log.info("anki-sync restarted")
        except Exception as e:
            log.error("Failed to restart anki-sync: %s", e)
        _write_lock.release()

@app.get("/health")
def health():
    return {"ok": True}
