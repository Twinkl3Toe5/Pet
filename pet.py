#!/usr/bin/env python3
"""PET: a persistent, rules-based artificial-life sandbox.

Run with: python pet.py
Then open http://127.0.0.1:8000
Only Python's standard library is required.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
DB_PATH = ROOT / "pet.sqlite3"
WORLD_W, WORLD_H = 900, 600
TICK_SECONDS = 1.0
lock = threading.RLock()

OBJECTS = [
    {"id": "berry_1", "kind": "berry_bush", "x": 170, "y": 160, "label": "Berry bush"},
    {"id": "berry_2", "kind": "berry_bush", "x": 700, "y": 430, "label": "Berry bush"},
    {"id": "pond", "kind": "pond", "x": 450, "y": 300, "label": "Pond"},
    {"id": "stone_1", "kind": "stone", "x": 300, "y": 430, "label": "Strange stone"},
    {"id": "flower_1", "kind": "flower", "x": 670, "y": 150, "label": "Wildflower"},
    {"id": "tree_1", "kind": "tree", "x": 100, "y": 450, "label": "Old tree"},
    {"id": "mushroom_1", "kind": "mushroom", "x": 520, "y": 100, "label": "Mushroom"},
]

def now():
    return datetime.now(timezone.utc).isoformat()

def connect():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db

def initialise():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK(id=1), tick INTEGER NOT NULL,
            x REAL NOT NULL, y REAL NOT NULL, energy REAL NOT NULL,
            hunger REAL NOT NULL, curiosity REAL NOT NULL, comfort REAL NOT NULL,
            knowledge INTEGER NOT NULL, discoveries TEXT NOT NULL,
            learned TEXT NOT NULL, born_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tick INTEGER NOT NULL,
            created_at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
        )""")
        row = db.execute("SELECT id FROM state WHERE id=1").fetchone()
        if row is None:
            db.execute("""INSERT INTO state VALUES
                (1,0,430,280,85,20,75,70,0,'[]','{}',?,?)""", (now(), now()))
            db.execute("INSERT INTO events(tick,created_at,kind,message) VALUES(0,?,?,?)",
                       (now(), "birth", "A new little creature opened its eyes. The world is unfamiliar."))
        db.commit()

def read_state():
    with connect() as db:
        row = dict(db.execute("SELECT * FROM state WHERE id=1").fetchone())
        row["discoveries"] = json.loads(row["discoveries"])
        row["learned"] = json.loads(row["learned"])
        return row

def log_event(tick, kind, message):
    with connect() as db:
        db.execute("INSERT INTO events(tick,created_at,kind,message) VALUES(?,?,?,?)",
                   (tick, now(), kind, message))
        db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 250)")
        db.commit()

def distance(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])

def simulation_step():
    with lock:
        s = read_state()
        s["tick"] += 1
        s["hunger"] = min(100, s["hunger"] + 0.65)
        s["energy"] = max(0, s["energy"] - 0.28)
        s["curiosity"] = min(100, s["curiosity"] + 0.12)
        s["comfort"] = max(0, s["comfort"] - 0.04)
        learned = s["learned"]
        discoveries = s["discoveries"]

        # Score possible intentions. Learned values bias future choices.
        candidates = []
        candidates.append((s["hunger"] * 1.25 + learned.get("eat", 0), "eat"))
        candidates.append(((100 - s["energy"]) * 1.2 + learned.get("rest", 0), "rest"))
        candidates.append((s["curiosity"] * 0.65 + learned.get("explore", 0), "explore"))
        action = max(candidates, key=lambda item: item[0])[1]
        message = None
        kind = "thought"

        if action == "eat":
            bushes = [o for o in OBJECTS if o["kind"] == "berry_bush"]
            target = min(bushes, key=lambda o: math.hypot(s["x"]-o["x"], s["y"]-o["y"]))
            d = distance(s, target)
            if d < 24:
                s["hunger"] = max(0, s["hunger"] - 38)
                learned["eat"] = learned.get("eat", 0) + 1.8
                message, kind = "Found berries and ate. That was useful; I should remember this place.", "food"
                if target["id"] not in discoveries:
                    discoveries.append(target["id"])
                    s["knowledge"] += 1
                    message = "Discovery: berries are edible! I have remembered this bush."
            else:
                move_towards(s, target, 2.7)
                message = "I'm hungry. Heading towards the berry bush."
        elif action == "rest":
            if s["energy"] > 96:
                s["comfort"] = min(100, s["comfort"] + 0.5)
                learned["rest"] = max(0, learned.get("rest", 0) - 0.1)
                message = "Resting quietly and watching the world."
            else:
                s["energy"] = min(100, s["energy"] + 1.8)
                message = "Taking a little rest."
        else:
            unknown = [o for o in OBJECTS if o["id"] not in discoveries]
            if unknown:
                target = min(unknown, key=lambda o: math.hypot(s["x"]-o["x"], s["y"]-o["y"]))
                if distance(s, target) < 22:
                    discoveries.append(target["id"])
                    s["knowledge"] += 1
                    s["curiosity"] = max(5, s["curiosity"] - 24)
                    learned["explore"] = learned.get("explore", 0) + 2.2
                    effects = {
                        "pond": "The pond is cool water. I wonder what lives in it.",
                        "stone": "A heavy stone. It doesn't move easily.",
                        "flower": "A bright flower. It has an interesting smell.",
                        "tree": "A very large tree. Its shade feels pleasant.",
                        "mushroom": "A strange mushroom. I don't know if it is safe, so I won't eat it.",
                        "berry_bush": "A bush with colourful berries. I should investigate those.",
                    }
                    message = "Discovery: " + effects.get(target["kind"], "I found something new.")
                    kind = "discovery"
                else:
                    move_towards(s, target, 2.3)
                    message = "Something nearby is unfamiliar. I want to investigate."
            else:
                # Once known things are exhausted, revisit them with a small chance
                # of reinforcing a preference rather than inventing new knowledge.
                target = random.choice(OBJECTS)
                move_towards(s, target, 1.8)
                message = "I've explored much of this place. I'm wandering and revisiting familiar spots."

        s["x"] = max(16, min(WORLD_W-16, s["x"]))
        s["y"] = max(16, min(WORLD_H-16, s["y"]))
        with connect() as db:
            db.execute("""UPDATE state SET tick=?,x=?,y=?,energy=?,hunger=?,curiosity=?,
                comfort=?,knowledge=?,discoveries=?,learned=?,updated_at=? WHERE id=1""",
                (s["tick"],s["x"],s["y"],s["energy"],s["hunger"],s["curiosity"],
                 s["comfort"],s["knowledge"],json.dumps(discoveries),json.dumps(learned),now()))
            db.commit()
        if message and (kind != "thought" or s["tick"] % 5 == 0):
            log_event(s["tick"], kind, message)

def move_towards(s, target, speed):
    dx, dy = target["x"]-s["x"], target["y"]-s["y"]
    d = math.hypot(dx, dy) or 1
    s["x"] += dx/d*min(speed,d)
    s["y"] += dy/d*min(speed,d)

def simulation_loop():
    while True:
        started = time.monotonic()
        try:
            simulation_step()
        except Exception as exc:
            log_event(read_state()["tick"], "error", f"Simulation recovered from an error: {exc}")
        time.sleep(max(0.05, TICK_SECONDS - (time.monotonic()-started)))

class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            with lock:
                s = read_state()
                with connect() as db:
                    events = [dict(r) for r in db.execute(
                        "SELECT tick,created_at,kind,message FROM events ORDER BY id DESC LIMIT 30")]
                self._json({"creature": {k:s[k] for k in ("tick","x","y","energy","hunger","curiosity","comfort","knowledge","born_at","updated_at")},
                            "discoveries":s["discoveries"],"learned":s["learned"],
                            "objects":OBJECTS,"events":events,"world":{"width":WORLD_W,"height":WORLD_H}})
            return
        if path in ("/", "/index.html"):
            content = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self._json({"error":"Not found"},404)

    def log_message(self, *_):
        pass

if __name__ == "__main__":
    initialise()
    thread = threading.Thread(target=simulation_loop, daemon=True)
    thread.start()
    print("PET is alive at http://127.0.0.1:8000")
    print("State is saved in pet.sqlite3. Press Ctrl+C to stop.")
    ThreadingHTTPServer(("127.0.0.1",8000), Handler).serve_forever()
