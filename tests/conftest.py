import os, sys, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# изоляция БД: свой файл на каждый запуск тестов
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "checks.db")
