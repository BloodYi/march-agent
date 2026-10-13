"""Read only the confirmed CLI 1.3.3 session schema fields for observed steps."""
from pathlib import Path
from contextlib import closing
import re
import sqlite3


MAX_BLOB = 1024 * 1024
# Generation envelopes also contain prompt/tool payloads. Only model and link
# fields are retained; step/thinking and aggregate scan limits stay unchanged.
MAX_GENERATION_BLOB = 8 * MAX_BLOB
MAX_FIELDS = 4096
MAX_STEPS = 2048
MAX_ROWS = 4096


def _varint(data, position):
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError("truncated varint")
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            raise ValueError("oversized varint")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError("oversized varint")


def _fields(data, wanted=None, *, max_blob=MAX_BLOB):
    if not isinstance(data, bytes) or len(data) > max_blob:
        raise ValueError("invalid metadata blob")
    fields, position = {}, 0
    count = 0
    while position < len(data):
        count += 1
        if count > MAX_FIELDS:
            raise ValueError("too many fields")
        tag, position = _varint(data, position)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < (1 << 29):
            raise ValueError("invalid field")
        if wire == 0:
            value, position = _varint(data, position)
        elif wire in (1, 2, 5):
            size = 8 if wire == 1 else 4
            if wire == 2:
                size, position = _varint(data, position)
            end = position + size
            if end > len(data):
                raise ValueError("truncated field")
            value, position = (data[position:end] if wanted is None or number in wanted else None), end
        else:
            raise ValueError("unsupported wire type")
        if wanted is None or number in wanted:
            fields.setdefault(number, []).append((wire, value))
    return fields


def _string(fields, number):
    values = fields.get(number, [])
    if not values:
        return None
    if len(values) != 1 or values[0][0] != 2:
        raise ValueError("invalid string field")
    value = values[0][1].decode("utf-8", errors="strict")
    if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid metadata string")
    return value


def _generation_link(data, observed_steps, execution_ids):
    try:
        root = _fields(data, {1, 2, 4}, max_blob=MAX_GENERATION_BLOB)
        indices = []
        for wire, value in root.get(2, []):
            if wire == 0:
                indices.append(value)
            elif wire == 2:
                position = 0
                while position < len(value):
                    index, position = _varint(value, position)
                    indices.append(index)
                    if len(indices) > MAX_STEPS:
                        raise ValueError("too many steps")
            else:
                raise ValueError("invalid step indices")
        if len(indices) > MAX_STEPS or not indices or any(index not in observed_steps for index in indices):
            return None
        execution = _string(root, 4)
        if execution_ids is None or not execution or any(
            execution_ids.get(index) != execution for index in indices
        ):
            return None
        chat = root.get(1, [])
        if not chat:
            return sorted(set(indices)), execution, None
        if len(chat) != 1 or chat[0][0] != 2:
            return None
        return sorted(set(indices)), execution, chat[0][1]
    except (ValueError, UnicodeDecodeError):
        return None


def _model_records(link):
    try:
        indices, execution, chat = link
        if chat is None:
            return []
        model = _fields(chat, {19, 22}, max_blob=MAX_GENERATION_BLOB)
        return [{"field": "gen_metadata.chat_model." + field, "value": value,
                 "source": "agy_session_db", "step_indices": sorted(set(indices)),
                 "execution_id": execution}
                for number, field in ((19, "response_model"), (22, "response_model_full"))
                if (value := _string(model, number)) is not None]
    except (ValueError, UnicodeDecodeError):
        return []


def generation_models(data, observed_steps, execution_ids=None):
    """No traversal: root 2 steps, root 4 execution, root 1 chat model 19/22."""
    link = _generation_link(data, observed_steps, execution_ids)
    return _model_records(link) if link is not None else []


def _session_path(home, conversation_id, observed_steps):
    if (not observed_steps or len(observed_steps) > MAX_STEPS
            or any(not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < (1 << 31)
                   for index in observed_steps)
            or not isinstance(conversation_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", conversation_id or ""
    )):
        return None
    path = Path(home) / ".gemini" / "antigravity-cli" / "conversations" / (conversation_id + ".db")
    return path if path.is_file() else None


def _step_executions(connection, observed_steps):
    execution_ids, total = {}, 0
    indices = sorted(observed_steps)
    for offset in range(0, len(indices), 512):
        batch = indices[offset:offset + 512]
        query = ("SELECT idx, metadata FROM steps WHERE idx IN (" + ",".join("?" for _ in batch)
                 + ") AND typeof(metadata)='blob' AND length(metadata) <= ?")
        for index, metadata in connection.execute(query, batch + [MAX_BLOB]):
            total += len(metadata)
            if total > 16 * MAX_BLOB:
                return None
            try:
                execution_ids[index] = _string(_fields(metadata, {12}), 12)
            except (ValueError, UnicodeDecodeError):
                pass
    return execution_ids


def _generation_links(connection, observed_steps, execution_ids):
    if execution_ids is None:
        return []
    links, total = [], 0
    rows = connection.execute("SELECT CASE WHEN typeof(data)='blob' AND length(data)<=? THEN data END "
                              "FROM gen_metadata LIMIT ?", (MAX_GENERATION_BLOB, MAX_ROWS + 1))
    for count, (data,) in enumerate(rows):
        if count >= MAX_ROWS:
            return []
        if not isinstance(data, bytes):
            continue
        total += len(data)
        if total > 16 * MAX_BLOB:
            return []
        link = _generation_link(data, observed_steps, execution_ids)
        if link is not None:
            links.append(link)
    return links


def read_session_models(home, conversation_id, observed_steps):
    path = _session_path(home, conversation_id, observed_steps)
    if path is None:
        return []
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
            connection.execute("BEGIN")
            links = _generation_links(connection, observed_steps, _step_executions(connection, observed_steps))
            return [record for link in links for record in _model_records(link)]
    except (sqlite3.Error, OSError, ValueError):
        return []


def read_session_historical_error(home, conversation_id, observed_steps, response_steps, error):
    """Confirm a CLI 1.3.3 aggregate error precedes this completed execution."""
    path = _session_path(home, conversation_id, observed_steps)
    if path is None or not response_steps or not set(response_steps).issubset(observed_steps):
        return None
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
            connection.execute("BEGIN")
            executions = _step_executions(connection, response_steps)
            if (executions is None or set(executions) != set(response_steps) or not all(executions.values())
                    or len(set(executions.values())) != 1):
                return None
            last = connection.execute("SELECT idx,step_type,status FROM steps ORDER BY idx DESC LIMIT 1").fetchone()
            # Native enum 15 = planner response, 3 = completed. A later step or
            # another concurrent execution makes the scope unprovable.
            if last != (max(response_steps), 15, 3):
                return None
            row = connection.execute("SELECT idx,step_payload FROM steps WHERE step_type=17 "
                                     "ORDER BY idx DESC LIMIT 1").fetchone()
            if row is None or row[0] >= min(observed_steps):
                return None
            payload = _fields(row[1], {1, 4, 24})
            if payload.get(1) != [(0, 17)] or payload.get(4) != [(0, 3)]:
                return None
            messages = payload.get(24, [])
            if len(messages) != 1 or messages[0][0] != 2:
                return None
            details = _fields(messages[0][1], {3}).get(3, [])
            if len(details) != 1 or details[0][0] != 2:
                return None
            # CortexStepErrorMessage.error(3) -> CortexErrorDetails.message(2).
            message = _string(_fields(details[0][1], {2}), 2)
            if message == error:
                return {"step_index": row[0], "error": message}
    except (sqlite3.Error, OSError, ValueError, UnicodeDecodeError, TypeError):
        pass
    return None


def planner_thinking(data):
    """gemini_coder.Step.planner_response (20).thinking (3); no signature/raw fields."""
    try:
        planners = _fields(data, {20}).get(20, [])
        if len(planners) != 1 or planners[0][0] != 2:
            return None
        thoughts = _fields(planners[0][1], {3}).get(3, [])
        if len(thoughts) != 1 or thoughts[0][0] != 2:
            return None
        text = thoughts[0][1].decode("utf-8", errors="strict")
        if not text.strip() or any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            return None
        return text
    except (ValueError, UnicodeDecodeError):
        return None


def read_session_thinking(home, conversation_id, response_steps, observed_steps=None):
    observed_steps = response_steps if observed_steps is None else observed_steps
    path = _session_path(home, conversation_id, observed_steps)
    if path is None or not response_steps or not set(response_steps).issubset(observed_steps):
        return []
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
            connection.execute("BEGIN")
            execution_ids = _step_executions(connection, observed_steps)
            links = _generation_links(connection, observed_steps, execution_ids)
            linked = {index for indices, _, _ in links for index in indices if index in response_steps}
            events, total = [], 0
            indices = sorted(linked)
            for offset in range(0, len(indices), 512):
                batch = indices[offset:offset + 512]
                query = ("SELECT idx, step_payload FROM steps WHERE idx IN (" + ",".join("?" for _ in batch)
                         + ") AND typeof(step_payload)='blob' AND length(step_payload) <= ? ORDER BY idx")
                for index, payload in connection.execute(query, batch + [MAX_BLOB]):
                    total += len(payload)
                    if total > 16 * MAX_BLOB:
                        return []
                    text = planner_thinking(payload)
                    if text is not None:
                        events.append({"event": "thinking", "mode": "snapshot", "step_index": index,
                                       "thinking": text, "source": "agy_session_db"})
            return events
    except (sqlite3.Error, OSError, ValueError):
        return []
