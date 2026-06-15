"""Send a push notification to your phone via ntfy.sh."""
import requests


def send_push(server, topic, title, message, url=None, priority="default"):
    """Send a push notification to an ntfy topic.

    server: e.g. "https://ntfy.sh"
    topic:  your private topic name
    title:  notification title
    message: notification body
    url:    optional link the notification opens when tapped
    priority: ntfy priority ("min", "low", "default", "high", "max")
    """
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
    }
    if url:
        # Tapping the notification opens this link.
        headers["Click"] = url

    resp = requests.post(
        f"{server.rstrip('/')}/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp


if __name__ == "__main__":
    # Quick manual test: python notify.py
    import json
    import pathlib

    cfg = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
    n = cfg["ntfy"]
    send_push(
        n["server"],
        n["topic"],
        title="Fixr Ping Messager",
        message="Test push - if you see this on your phone, notifications work!",
        priority="high",
    )
    print(f"Sent test push to topic '{n['topic']}' on {n['server']}")
