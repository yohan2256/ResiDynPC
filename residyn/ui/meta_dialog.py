"""성적서 정보 입력 다이얼로그.

시편번호처럼 측정마다 바뀌는 항목이 있어 설정 화면이 아니라 성적서를 뽑는
시점에 받는다. 직전 입력을 초기값으로 넣어, 같은 로트의 시편 2·3번을 이어
측정할 때 번호만 고치면 되게 한다.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from ..report import ReportMeta


class MetaDialog(QDialog):
    def __init__(self, prev: ReportMeta, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("성적서 정보 입력")
        self.setMinimumWidth(380)

        self.site = QLineEdit(prev.site_name)
        self.unit = QLineEdit(prev.unit_no)
        self.maker = QLineEdit(prev.maker)
        self.product = QLineEdit(prev.product_name)
        self.lot = QLineEdit(prev.lot_no)
        self.operator = QLineEdit(prev.operator)

        self.spec_no = QSpinBox()
        self.spec_no.setRange(1, 999)
        self.spec_no.setValue(prev.specimen_no)
        self.spec_total = QSpinBox()
        self.spec_total.setRange(1, 999)
        self.spec_total.setValue(prev.specimen_total)

        form = QFormLayout()
        form.addRow("현장명", self.site)
        form.addRow("동·호수", self.unit)
        form.addRow("제조사", self.maker)
        form.addRow("품명", self.product)
        form.addRow("로트번호", self.lot)
        form.addRow("시편번호", self.spec_no)
        form.addRow("시편 수", self.spec_total)
        form.addRow("측정자", self.operator)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("성적서 생성")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        hint = QLabel("비워두면 성적서에 '-'로 표기됩니다.")
        hint.setStyleSheet("color: #888;")
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def value(self) -> ReportMeta:
        return ReportMeta(
            site_name=self.site.text().strip(),
            unit_no=self.unit.text().strip(),
            maker=self.maker.text().strip(),
            product_name=self.product.text().strip(),
            lot_no=self.lot.text().strip(),
            specimen_no=self.spec_no.value(),
            specimen_total=self.spec_total.value(),
            operator=self.operator.text().strip(),
        )
