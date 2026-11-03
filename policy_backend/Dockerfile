FROM python:3.12-slim

WORKDIR /service
COPY policy_backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY policy_backend ./policy_backend
CMD ["uvicorn", "policy_backend.app:app", "--host", "0.0.0.0", "--port", "8080"]
