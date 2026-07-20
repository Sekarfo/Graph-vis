"""Стартует контейнер от root (нужно для chown смонтированного volume),
чинит владельца DATA_DIR под appuser и сразу же роняет привилегии перед
запуском uvicorn. os.execvp заменяет процесс на месте, поэтому всё окружение
(DATABASE_URL, EDIT_PASSWORD, DATA_DIR, PORT...) доходит до приложения как есть
— в отличие от su/sudo, тут ничего не нужно прокидывать вручную."""
import os
import pwd

APPUSER = "appuser"

data_dir = os.environ.get("DATA_DIR")
if data_dir:
    pw = pwd.getpwnam(APPUSER)
    os.makedirs(data_dir, exist_ok=True)
    for root, dirs, files in os.walk(data_dir):
        os.chown(root, pw.pw_uid, pw.pw_gid)
        for name in dirs + files:
            os.chown(os.path.join(root, name), pw.pw_uid, pw.pw_gid)
    os.chown(data_dir, pw.pw_uid, pw.pw_gid)

pw = pwd.getpwnam(APPUSER)
os.setgid(pw.pw_gid)
os.setuid(pw.pw_uid)

port = os.environ.get("PORT", "8080")
os.execvp("uvicorn", ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", port])
