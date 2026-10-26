FROM python:3.11-slim-bookworm
 
WORKDIR /app

ENV PYTHONPATH=/app

# 1. Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    ca-certificates \
    apt-transport-https \
    unixodbc \
    unixodbc-dev \
    gcc \
    g++ \
    xvfb \
    
&& rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*
 
# 2. Add Microsoft repo (FIXED - no apt-key)
RUN mkdir -p /etc/apt/keyrings \
&& curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
&& echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list
 
# 3. Install ODBC Driver 17 (matches your code)
RUN apt-get update \
&& ACCEPT_EULA=Y apt-get install -y msodbcsql17 \
&& rm -rf /var/lib/apt/lists/*

 
# 4. Install Python dependencies
COPY requirement.txt .
RUN pip install --no-cache-dir -r requirement.txt
 
# 5. Install Playwright + dependencies
RUN pip install playwright \
&& playwright install chromium \
&& playwright install-deps chromium
 
# 6. Copy project files
COPY . .
 
# 7. Run your script
CMD ["python", "run.py"]
