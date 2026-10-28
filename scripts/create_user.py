import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.session import Base, SessionLocal, engine
from app.models.resource import Resource  # noqa: F401
from app.models.user import User
from app.services.security import hash_password


def main() -> None:
    Base.metadata.create_all(bind=engine)
    email = input("Email: ").strip().lower()
    name = input("Name: ").strip()
    department = input("Department (optional): ").strip() or None
    roles = [
        role.strip()
        for role in input("Roles (comma-separated): ").split(",")
        if role.strip()
    ]
    password = getpass("Temporary password: ")
    if not email or not name or not password:
        raise SystemExit("Email, name, and password are required.")

    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.email == email)) is not None:
            raise SystemExit("A user with this email already exists.")
        db.add(
            User(
                email=email,
                name=name,
                department=department,
                password_hash=hash_password(password),
                roles=roles,
            )
        )
        db.commit()
    print(f"Created user: {email}")


if __name__ == "__main__":
    main()
