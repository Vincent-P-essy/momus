import asyncio


async def send(email, message):
    await asyncio.sleep(0)
    return f"sent {message} to {email}"


async def notify_all(emails, message):
    for email in emails:
        send(email, message)
