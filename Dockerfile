# Alex — US equity screener. Stdlib-only Python, no build step.
FROM python:3.12-slim

WORKDIR /app
COPY server.py home.html index.html universe.json favicon.ico ./

# Cloud: listen on all interfaces; the platform routes to this port.
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080

CMD ["python", "server.py", "--no-browser"]
