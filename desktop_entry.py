"""PyInstaller entry point; keep package-relative imports working."""
from sub2loc.desktop import main
raise SystemExit(main())
