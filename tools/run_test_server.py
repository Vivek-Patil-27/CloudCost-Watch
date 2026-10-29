r"""Start CloudCost Watch with a separate, throw-away test database
(tools/test_server.db), so browser checks never touch your real data.

    py tools\run_test_server.py
Then open http://127.0.0.1:5000. Stop it with Ctrl + C.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "test_server.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)  # start fresh every time

sys.path.insert(0, os.path.dirname(HERE))
import app as cc  # noqa: E402

cc.DATABASE = TEST_DB
cc.init_db()
cc.app.run(port=5000, debug=False)
