"""Start the web service with the local .env configuration."""
import os
import app
import uvicorn

if __name__ == '__main__':
    if not app.BASIC_AUTH_USER or not app.BASIC_AUTH_PASS:
        raise SystemExit('Set BASIC_AUTH_USER and BASIC_AUTH_PASS in .env before starting. / 请先在 .env 填写网页登录账号和密码。')
    uvicorn.run(app.app, host=os.getenv('HOST','127.0.0.1'), port=app.PORT)
