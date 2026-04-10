from collections import deque
from datetime import datetime

live_events = deque(maxlen=100)
event_subscribers = []


async def broadcast_event(event_type, data):
    event = {
        "type": event_type,
        "data": data,
        "timestamp": datetime.utcnow().isoformat(),
    }
    live_events.append(event)
    for queue in list(event_subscribers):
        try:
            await queue.put(event)
        except Exception:
            pass
