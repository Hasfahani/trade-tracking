import asyncio
import json
from collections import deque
from datetime import datetime

# Global event queue for live updates
live_events = deque(maxlen=100)
event_subscribers = []

async def broadcast_event(event_type, data):
    '''Broadcast an event to all SSE subscribers'''
    event = {
        'type': event_type,
        'data': data,
        'timestamp': datetime.utcnow().isoformat()
    }
    live_events.append(event)
    # Notify all subscribers
    for queue in event_subscribers:
        try:
            await queue.put(event)
        except:
            pass
