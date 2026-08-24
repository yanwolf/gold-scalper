FROM python:3.11-slim

WORKDIR /app

# 先複製 requirements 再安裝，可以利用 Docker layer cache，之後改程式碼不用重裝套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Zeabur 會注入 PORT 環境變數，容器內服務要監聽這個 port
ENV PORT=8000
EXPOSE 8000

CMD ["python", "-m", "app.main"]
