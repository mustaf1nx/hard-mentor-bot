import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from database import SessionLocal, Application, init_db

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

app = FastAPI(title="Hard-Mentor Admin")
templates = Jinja2Templates(directory="templates")

init_db()

STATUS_LABELS = {"new": "новая", "in_progress": "в работе", "closed": "закрыта"}


async def notify_student(telegram_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": telegram_id, "text": text})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, status: Optional[str] = None):
    db = SessionLocal()
    try:
        query = db.query(Application)
        if status:
            query = query.filter(Application.status == status)
        applications = query.order_by(Application.created_at.desc()).all()
    finally:
        db.close()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "applications": applications,
            "status": status,
            "status_labels": STATUS_LABELS,
        },
    )


@app.post("/applications/{app_id}/update")
async def update_application(
    app_id: int,
    status: str = Form(...),
    assigned_mentor: str = Form(""),
):
    db = SessionLocal()
    try:
        application = db.query(Application).filter(Application.id == app_id).first()
        if not application:
            raise HTTPException(status_code=404, detail="Заявка не найдена")

        was_closed_now = status == "closed" and application.status != "closed"

        application.status = status
        application.assigned_mentor = assigned_mentor or None
        application.updated_at = datetime.utcnow()
        db.commit()

        telegram_id = application.telegram_id
        subject = application.subject
        mentor = application.assigned_mentor
    finally:
        db.close()

    if was_closed_now and mentor:
        text = (
            f"Хорошие новости! По предмету «{subject}» "
            f"тебе назначен хард-ментор: {mentor} 🎉"
        )
        await notify_student(telegram_id, text)

    return RedirectResponse(url="/", status_code=303)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
