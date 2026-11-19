
"""Real sqlite3 store: content-hash primary key, INSERT-OR-IGNORE dedup, watermark.

Also persists the identity-resolution journal (`identity_decisions`) and
human review decisions (`identity_overrides`).  Both tables are additive:
existing databases and callers keep working unchanged.
"""
import json, sqlite3
from datetime import datetime, timezone
from .config import DB_PATH

class Store:
    def __init__(self, con=None):
        self.con = con
        if con is not None:
            self._init()

    def connect(self):
        con = sqlite3.connect(DB_PATH)
        self.__init__(con)
        return con

    def close(self):
        self.con.close()

    def _init(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS records("
            "content_hash TEXT PRIMARY KEY, id TEXT, ts TEXT, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS identity_decisions("
            "pair_key TEXT NOT NULL, rule_version TEXT NOT NULL, state TEXT NOT NULL,"
            "reg_key TEXT, a_id TEXT, b_id TEXT, a_key TEXT, b_key TEXT,"
            "evidence TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,"
            "PRIMARY KEY(pair_key, rule_version))")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS identity_overrides("
            "reg_key TEXT PRIMARY KEY, decision TEXT NOT NULL,"
            "note TEXT DEFAULT '', created_at TEXT NOT NULL)")
        self.con.commit()

    @staticmethod
    def hash(record):
        blob = json.dumps(record, sort_keys=True, ensure_ascii=False)
        import hashlib
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def upsert(self, record):
        cur = self.con.execute(
            "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) VALUES(?,?,?,?)",
            (self.hash(record), str(record.get("id", "")),
             str(record.get("ts", "")), json.dumps(record, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount > 0

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def all_records(self):
        return [json.loads(p) for (_h, p) in
                self.con.execute("SELECT content_hash,payload FROM records")]

    def get(self, content_hash):
        row = self.con.execute(
            "SELECT payload FROM records WHERE content_hash=?", (content_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def watermark(self):
        row = self.con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
        return row[0] if row else None

    def set_watermark(self, value):
        self.con.execute(
            "INSERT INTO meta(k,v) VALUES('watermark',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(value),))
        self.con.commit()

    def reset(self):
        self.con.execute("DELETE FROM records")
        self.con.execute("DELETE FROM meta")
        self.con.execute("DELETE FROM identity_decisions")
        self.con.execute("DELETE FROM identity_overrides")
        self.con.commit()

    # ------------------------------------------------------------------ #
    # Identity journal and human decisions.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def record_decision(self, row):
        """Upsert one rule decision; ``created_at`` keeps first-seen time."""
        self.con.execute(
            "INSERT INTO identity_decisions(pair_key,rule_version,state,reg_key,"
            "a_id,b_id,a_key,b_key,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pair_key,rule_version) DO UPDATE SET state=excluded.state,"
            "reg_key=excluded.reg_key, a_id=excluded.a_id, b_id=excluded.b_id,"
            "a_key=excluded.a_key, b_key=excluded.b_key, evidence=excluded.evidence",
            (row["pair_key"], row["rule_version"], row["state"],
             row.get("reg_key", ""), row.get("a_id", ""), row.get("b_id", ""),
             row.get("a_key", ""), row.get("b_key", ""),
             json.dumps(row.get("evidence", []), ensure_ascii=False), self._now()))
        self.con.commit()

    def decisions(self, pair_key=None):
        sql = ("SELECT pair_key,rule_version,state,reg_key,a_id,b_id,a_key,b_key,"
               "evidence,created_at FROM identity_decisions")
        args = ()
        if pair_key is not None:
            sql += " WHERE pair_key=?"
            args = (pair_key,)
        sql += " ORDER BY pair_key,rule_version"
        out = []
        for t in self.con.execute(sql, args):
            out.append({"pair_key": t[0], "rule_version": t[1], "state": t[2],
                        "reg_key": t[3], "a_id": t[4], "b_id": t[5],
                        "a_key": t[6], "b_key": t[7],
                        "evidence": json.loads(t[8]), "created_at": t[9]})
        return out

    def record_override(self, reg_key, decision, note=""):
        self.con.execute(
            "INSERT INTO identity_overrides(reg_key,decision,note,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(reg_key) DO UPDATE SET "
            "decision=excluded.decision, note=excluded.note",
            (reg_key, decision, note, self._now()))
        self.con.commit()

    def clear_override(self, reg_key):
        self.con.execute("DELETE FROM identity_overrides WHERE reg_key=?", (reg_key,))
        self.con.commit()

    def overrides(self):
        return {r[0]: {"decision": r[1], "note": r[2]} for r in
                self.con.execute("SELECT reg_key,decision,note FROM identity_overrides")}
