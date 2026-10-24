from .llm import load_env_file

load_env_file()

from .app import main

raise SystemExit(main())
