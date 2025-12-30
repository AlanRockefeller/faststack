try:
    with open(r"c:\code\dikarya\part11_request.md", "r", encoding="utf-8") as f:
        print(f.read())
except Exception as e:
    print(f"Error: {e}")
