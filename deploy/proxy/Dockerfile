FROM certbot/certbot:v5.10.0
RUN apk add --no-cache nginx openssl ca-certificates \
    && mkdir -p /run/nginx /var/www/acme /var/lib/nginx/tmp
COPY main.py /opt/proxy/main.py
COPY *.conf /opt/proxy/
ENTRYPOINT ["python", "/opt/proxy/main.py"]
