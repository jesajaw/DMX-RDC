import tkinter as tk

from src import ui
from src.controller import enable_dpi_awareness


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    root.protocol("WM_DELETE_WINDOW", ui.DMXUI(root).on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
