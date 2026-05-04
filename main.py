from playwright.sync_api import sync_playwright
import json, csv, time


OUTPUT_FILE = "nobitex_transfers.json"

def to_csv(data):
    keys = data[0].keys()

    with open("nobitex.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(data)

def run():
    with sync_playwright() as p:
        # 🔑 persistent session (VERY IMPORTANT)
        context = p.chromium.launch_persistent_context(
            user_data_dir="./user_data",
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        )

        page = context.pages[0] if context.pages else context.new_page()

        all_data = []

        # 🧠 capture real API responses
        def handle_response(response):
            if "api.arkm.com/transfers" in response.url:
                try:
                    data = response.json()
                    transfers = data.get("transfers", [])

                    if transfers:
                        print(f"Captured {len(transfers)} transfers")
                        all_data.extend(transfers)

                except Exception as e:
                    print("Parse error:", e)

        page.on("response", handle_response)

        # 🌐 open site
        page.goto(
            "https://intel.arkm.com/explorer/entity/nobitex",
            wait_until="domcontentloaded",
            timeout=60000
        )

        print("👉 If Cloudflare appears, solve it manually once...")

        # ⏳ give time for manual verification
        time.sleep(10)

        # 🔁 trigger loading more data
        for i in range(15):
            print(f"Scrolling... {i+1}")
            page.mouse.wheel(0, 8000)
            time.sleep(2)

        # 💾 save result
        with open(OUTPUT_FILE, "w") as f:
            json.dump(all_data, f, indent=2)

        print(f"✅ Saved {len(all_data)} transfers")

        context.close()


if __name__ == "__main__":
    run()