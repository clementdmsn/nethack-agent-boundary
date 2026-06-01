from app.runner import Runner


def main() -> None:
    # Starts the interactive runtime.
    runner = Runner()
    runner.run()


if __name__ == "__main__":
    main()
