from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from core.anonymizer import anonymize
from core.restorer import restore
from storage import project_store
from storage.models import ProjectMapping


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("QueryAnon - SQL 쿼리 익명화 도구")
        self.geometry("900x700")

        self.project: ProjectMapping | None = None

        self._build_project_bar()
        self._build_tabs()

    def _build_project_bar(self) -> None:
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")

        ttk.Label(bar, text="프로젝트:").pack(side="left")

        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(bar, textvariable=self.project_var, width=30)
        self.project_combo["values"] = project_store.list_projects()
        self.project_combo.pack(side="left", padx=4)

        ttk.Button(bar, text="선택/생성", command=self._on_select_project).pack(side="left", padx=4)

        self.project_status = ttk.Label(bar, text="프로젝트 미선택", foreground="red")
        self.project_status.pack(side="left", padx=12)

    def _on_select_project(self) -> None:
        name = self.project_var.get().strip()
        if not name:
            messagebox.showwarning("QueryAnon", "프로젝트 이름을 입력하세요.")
            return
        self.project = project_store.load(name)
        self.project_status.config(
            text=f"'{name}' 로드됨 (매핑 {len(self.project.entries)}건)", foreground="green"
        )
        values = list(self.project_combo["values"])
        if name not in values:
            self.project_combo["values"] = values + [name]

    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        anon_tab = ttk.Frame(notebook)
        restore_tab = ttk.Frame(notebook)
        notebook.add(anon_tab, text="익명화")
        notebook.add(restore_tab, text="복원")

        self._build_anonymize_tab(anon_tab)
        self._build_restore_tab(restore_tab)

    def _build_anonymize_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="원본 쿼리").pack(anchor="w")
        self.anon_input = tk.Text(parent, height=15, wrap="word")
        self.anon_input.pack(fill="both", expand=True, pady=(0, 4))

        mode_row = ttk.Frame(parent)
        mode_row.pack(fill="x", pady=(0, 4))
        ttk.Label(mode_row, text="모드:").pack(side="left")
        self.anon_mode = tk.StringVar(value="mybatis")
        ttk.Radiobutton(
            mode_row, text="MyBatis 매퍼", variable=self.anon_mode, value="mybatis"
        ).pack(side="left", padx=4)
        ttk.Radiobutton(
            mode_row,
            text="단순 SQL (리터럴 값도 익명화)",
            variable=self.anon_mode,
            value="plain",
        ).pack(side="left", padx=4)

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=4)
        ttk.Button(btn_row, text="익명화 실행", command=self._on_anonymize).pack(side="left")
        ttk.Button(btn_row, text="결과 복사", command=lambda: self._copy(self.anon_output)).pack(
            side="left", padx=4
        )

        ttk.Label(parent, text="익명화 결과 (LLM에 붙여넣을 내용)").pack(anchor="w")
        self.anon_output = tk.Text(parent, height=15, wrap="word", state="disabled")
        self.anon_output.pack(fill="both", expand=True)

    def _build_restore_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="LLM 응답 (익명화된 토큰 포함)").pack(anchor="w")
        self.restore_input = tk.Text(parent, height=15, wrap="word")
        self.restore_input.pack(fill="both", expand=True, pady=(0, 4))

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=4)
        ttk.Button(btn_row, text="복원 실행", command=self._on_restore).pack(side="left")
        ttk.Button(btn_row, text="결과 복사", command=lambda: self._copy(self.restore_output)).pack(
            side="left", padx=4
        )

        ttk.Label(parent, text="복원 결과 (원본 이름으로 복구됨)").pack(anchor="w")
        self.restore_output = tk.Text(parent, height=15, wrap="word", state="disabled")
        self.restore_output.pack(fill="both", expand=True)

    def _require_project(self) -> ProjectMapping | None:
        if self.project is None:
            messagebox.showwarning("QueryAnon", "먼저 프로젝트를 선택/생성하세요.")
            return None
        return self.project

    def _on_anonymize(self) -> None:
        project = self._require_project()
        if project is None:
            return
        sql = self.anon_input.get("1.0", "end").strip()
        if not sql:
            return
        anonymize_literals = self.anon_mode.get() == "plain"
        try:
            result, dynamic_tag_count = anonymize(sql, project, anonymize_literals=anonymize_literals)
        except Exception as exc:
            messagebox.showerror("QueryAnon", f"익명화 실패 (SQL 파싱 오류 가능성):\n{exc}")
            return
        project_store.save(project)
        self.project_status.config(
            text=f"'{project.project_name}' 로드됨 (매핑 {len(project.entries)}건)", foreground="green"
        )
        self._set_text(self.anon_output, result)
        if dynamic_tag_count > 0:
            messagebox.showwarning(
                "QueryAnon",
                f"<isNotEmpty> 등 동적 SQL 태그 {dynamic_tag_count}개가 감지되었습니다.\n"
                "마스킹/복원 내용은 정확하지만, 태그의 정확한 위치는 원본과 달라질 수 있습니다.\n"
                "복원 결과를 매퍼 파일에 다시 붙여넣기 전에 태그 위치를 직접 확인하세요.",
            )

    def _on_restore(self) -> None:
        project = self._require_project()
        if project is None:
            return
        text = self.restore_input.get("1.0", "end").strip()
        if not text:
            return
        result = restore(text, project)
        self._set_text(self.restore_output, result)

    @staticmethod
    def _set_text(widget: tk.Text, content: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.config(state="disabled")

    def _copy(self, widget: tk.Text) -> None:
        content = widget.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(content)


def run() -> None:
    app = App()
    app.mainloop()
