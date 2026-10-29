r"""Automatic browser checks for CloudCost Watch (24 checks).

Opens an invisible Google Chrome window, clicks through the app like a user
and saves screenshots. Needs Google Chrome plus:  py -m pip install requests websocket-client

Check your own laptop (start tools\run_test_server.py first, in another terminal):
    py tools\browser_check.py
Check the live website:
    py tools\browser_check.py https://vivekpatil2027.pythonanywhere.com live_

It signs in as the test user "site_checker" (creating it if needed) and saves
three sample estimates to that user.
"""
import base64, json, os, subprocess, sys, tempfile, time
import requests, websocket

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
PREFIX = sys.argv[2] if len(sys.argv) > 2 else ""
SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_shots")
os.makedirs(SHOTS, exist_ok=True)
PROFILE = tempfile.mkdtemp(prefix="cloudcost_chrome_")
CHROME = next((p for p in [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
] if os.path.exists(p)), None)
if not CHROME:
    sys.exit("Google Chrome (or Microsoft Edge) was not found.")

proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9333",
                         "--user-data-dir=" + PROFILE,
                         "--no-first-run", "--hide-scrollbars", "about:blank"])
for _ in range(50):
    try:
        tabs = requests.get("http://127.0.0.1:9333/json").json()
        page = [t for t in tabs if t["type"] == "page"][0]
        break
    except Exception:
        time.sleep(0.2)
ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True)
msg_id = 0

def cdp(method, **params):
    global msg_id
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    while True:
        reply = json.loads(ws.recv())
        if reply.get("id") == msg_id:
            return reply.get("result", reply)

def js(expr):
    r = cdp("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
    return r.get("result", {}).get("value")

def wait_load():
    time.sleep(0.4)
    for _ in range(50):
        if js("document.readyState") == "complete":
            break
        time.sleep(0.1)
    time.sleep(1.2)  # chart animations

def go(url):
    cdp("Page.navigate", url=url)
    wait_load()

def viewport(w, h, mobile=False):
    cdp("Emulation.setDeviceMetricsOverride", width=w, height=h,
        deviceScaleFactor=1, mobile=mobile)

def shot(name):
    r = cdp("Page.captureScreenshot", format="png", captureBeyondViewport=True)
    with open(os.path.join(SHOTS, PREFIX + name), "wb") as f:
        f.write(base64.b64decode(r["data"]))

def submit(vm, db, days, budget):
    js(f"""(() => {{
        const v = {{vm_hours:'{vm}', db_hours:'{db}', days:'{days}', budget:'{budget}'}};
        for (const k in v) document.getElementById(k).value = v[k];
        document.querySelector('form[action="/"]').submit();
    }})()""")
    wait_load()

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

cdp("Page.enable"); cdp("Runtime.enable")
viewport(1366, 900)

go(BASE)
check("Redirected to sign-in when logged out", "/login" in js("location.pathname"))
shot("00_login.png")
js("document.querySelector('a[href=\"/register\"]').click()"); wait_load()
js("""(() => { const f = id => document.getElementById(id); f('username').value='site_checker'; f('password').value='checker-pass-2026'; f('confirm').value='checker-pass-2026';
      document.querySelector('form').submit(); })()"""); wait_load()
if "already taken" in js("document.body.innerText"):
    go(BASE + "/login")
    js("""(() => { const f = id => document.getElementById(id); f('username').value='site_checker';
          f('password').value='checker-pass-2026'; document.querySelector('form').submit(); })()"""); wait_load()
check("Register / sign in works", "Signed in as site_checker" in js("document.body.innerText"))
check("Dashboard loads", "Cost estimator" in js("document.body.innerText"))
shot("01_empty.png")

submit(6, 4, 26, 2000)
text = js("document.body.innerText")
check("Default total ₹1,092.00", "₹1,092.00" in text)
LATEST = js("location.search")
check("Green state", "Within budget" in text and js("!!document.querySelector('.progress-fill.good')"))
check("Cost doughnut chart drawn", js("!!(window.costChart && window.costChart.data.datasets[0].data.join() === '780,312')"),
      js("window.costChart ? window.costChart.data.datasets[0].data.join() : 'missing'"))
check("History chart drawn", js("!!window.historyChart"))
check("Resource chart drawn", js("!!window.resourceChart"))
shot("02_green_light.png")

submit(8, 0, 2, 100)
text = js("document.body.innerText")
check("Orange state at 80%", "Near budget limit" in text and js("!!document.querySelector('.progress-fill.warning-fill')"))
shot("03_orange.png")

submit(6, 4, 26, 500)
text = js("document.body.innerText")
check("Red state over budget", "Over budget" in text and "exceeds your budget by ₹592.00" in text)
check("History chart shows the new estimates", js("window.historyChart.data.datasets[0].data.slice(-3).join()") == "1092,80,1092",
      str(js("window.historyChart.data.datasets[0].data")))
shot("04_red.png")

submit(-3, 4, 26, 2000)
check("Invalid input error shown", "VM hours must be between 0 and 24." in js("document.body.innerText"))

# Theme toggle
go(BASE + "/" + LATEST)
before = js("getComputedStyle(document.body).backgroundColor")
js("document.getElementById('themeToggle').click()")
time.sleep(0.8)
after = js("getComputedStyle(document.body).backgroundColor")
legend = js("window.costChart.options.plugins.legend.labels.color")
check("Dark mode switches background", before != after, f"{before} -> {after}")
check("Chart text recoloured in dark mode", legend == "#edf2ff", legend)
check("Status text readable in dark mode",
      js("getComputedStyle(document.querySelector('.status-message')).color") == "rgb(134, 239, 172)",
      js("getComputedStyle(document.querySelector('.status-message')).color"))
check("Button label changes", "Day mode" in js("document.getElementById('themeToggle').textContent"))
shot("05_dark.png")
go(BASE + "/" + LATEST)
check("Dark mode remembered after reload", js("document.body.classList.contains('dark')"))
js("document.getElementById('themeToggle').click()")
time.sleep(0.5)

# Mobile layout
viewport(390, 844, mobile=True)
go(BASE + "/" + LATEST)
check("No horizontal page scroll on phone",
      js("document.documentElement.scrollWidth <= window.innerWidth"),
      f"{js('document.documentElement.scrollWidth')} vs {js('window.innerWidth')}")
check("Layout stacks to one column",
      js("getComputedStyle(document.querySelector('.layout')).gridTemplateColumns.split(' ').length") == 1)
shot("06_mobile.png")

check("Short history labels", js("window.historyChart.data.labels[0]").count(":") == 1,
      js("window.historyChart.data.labels[0]"))

# Chart.js blocked -> friendly message
viewport(1366, 900)
cdp("Network.enable")
cdp("Network.setBlockedURLs", urls=["*chart.umd.min.js*"])
go(BASE + "/" + LATEST)
check("Message shown if Chart.js fails", "Charts could not load" in js("document.body.innerText"))
shot("07_offline.png")
cdp("Network.setBlockedURLs", urls=[])

# CSV (download with the browser's login cookie)
cookies = {c["name"]: c["value"] for c in cdp("Network.getAllCookies")["cookies"]}
csv_text = requests.get(BASE + "/export", cookies=cookies).text
lines = [l for l in csv_text.splitlines() if l]
check("CSV has header + estimates", len(lines) >= 4 and lines[0].startswith("Date and time"), f"{len(lines)} lines")

viewport(1366, 900)
go(BASE)
js("document.querySelector('.header-actions form').submit()"); wait_load()
check("Sign out returns to sign-in", "/login" in js("location.pathname"))
go(BASE + "/" + LATEST)
check("Dashboard blocked after sign-out", "/login" in js("location.pathname"))

errors = js("window.__errs || []")
ws.close(); proc.terminate()

print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed")
