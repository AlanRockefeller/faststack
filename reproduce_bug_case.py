from pathlib import Path


def test_path_equality():
    p1 = Path("c:/code/faststack/test.jpg")
    p2 = Path("C:/code/faststack/test.jpg")

    print(f"p1: {p1}")
    print(f"p2: {p2}")
    print(f"p1 == p2: {p1 == p2}")

    p3 = Path("c:\\code\\faststack\\test.jpg")
    print(f"p3: {p3}")
    print(f"p1 == p3: {p1 == p3}")


if __name__ == "__main__":
    test_path_equality()
