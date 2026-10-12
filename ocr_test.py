import base64
import requests
import json
import os
import io
import sys
import re
from pdf2image import convert_from_path
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QTableWidget, QTableWidgetItem, 
                             QFileDialog, QLabel, QMessageBox, QHeaderView, QAbstractItemView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QRect, QPoint, QSize, QBuffer, QByteArray
from PyQt6.QtGui import QFont, QPixmap, QPainter, QPen, QColor, QImage, QTransform

# ==============================================================================
# [설정 파일 및 경로 입력]
# ==============================================================================
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
BASE_DIR = os.getcwd()
POPPLER_PATH = os.path.join(BASE_DIR, "poppler", "Library", "bin")
DATASET_DIR = os.path.join(BASE_DIR, "training_dataset")

if not os.path.exists(DATASET_DIR):
    os.makedirs(DATASET_DIR)

# ==============================================================================
# [고해상도 인터랙티브 이미지 캔버스 정의]
# ==============================================================================
class DrawingCanvas(QLabel):
    drag_finished_signal = pyqtSignal(QRect, QRect)

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setStyleSheet("background-color: #ECECEC; border: 1px solid #B0B0B0;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(100, 100)
        self.setScaledContents(False)
        
        self.original_pixmap = None
        self.zoom_factor = 1.0
        self.pan_offset = QPoint(0, 0)
        self.last_mouse_pos = QPoint()
        
        self.begin = QPoint()
        self.end = QPoint()
        self.is_cropping = False
        self.is_panning = False
        
        # 💡 시각화 검수용 절대 픽셀 좌표 보관 변수
        self.highlight_box_pixel = None 

    def set_image(self, qimage):
        self.original_pixmap = QPixmap.fromImage(qimage)
        self.zoom_factor = 1.0
        self.pan_offset = QPoint(0, 0)
        self.highlight_box_pixel = None
        self.update()

    def set_highlight_box(self, box_pixel):
        """외부(테이블 행 선택)에서 절대 픽셀 좌표 주입 시 호출"""
        self.highlight_box_pixel = box_pixel
        self.update()

    def get_combined_transform(self):
        transform = QTransform()
        if not self.original_pixmap:
            return transform
        win_w, win_h = self.width(), self.height()
        img_w = self.original_pixmap.width() * self.zoom_factor
        img_h = self.original_pixmap.height() * self.zoom_factor
        base_x = (win_w - img_w) / 2 + self.pan_offset.x()
        base_y = (win_h - img_h) / 2 + self.pan_offset.y()
        transform.translate(base_x, base_y)
        transform.scale(self.zoom_factor, self.zoom_factor)
        return transform
    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and self.original_pixmap:
            old_zoom = self.zoom_factor
            if event.angleDelta().y() > 0:
                self.zoom_factor *= 1.15
            else:
                self.zoom_factor /= 1.15
            self.zoom_factor = max(0.1, min(self.zoom_factor, 10.0))
            mouse_pos = event.position().toPoint()
            mouse_at_img = (mouse_pos - self.pan_offset - QPoint(int(self.width()/2), int(self.height()/2))) / old_zoom
            self.pan_offset += (mouse_at_img * old_zoom - mouse_at_img * self.zoom_factor)
            self.update()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if not self.original_pixmap:
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.last_mouse_pos = event.position().toPoint()
            self.is_panning = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton:
            self.begin = event.position().toPoint()
            self.end = event.position().toPoint()
            self.is_cropping = True

    def mouseMoveEvent(self, event):
        if self.is_panning:
            delta = event.position().toPoint() - self.last_mouse_pos
            self.pan_offset += delta
            self.last_mouse_pos = event.position().toPoint()
            self.update()
        elif self.is_cropping:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton and self.is_panning:
            self.is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.button() == Qt.MouseButton.LeftButton and self.is_cropping:
            self.end = event.position().toPoint()
            self.is_cropping = False
            self.update()
            
            screen_rect = QRect(self.begin, self.end).normalized()
            if screen_rect.width() > 5 and screen_rect.height() > 5 and self.original_pixmap:
                inv_trans, success = self.get_combined_transform().inverted()
                if success:
                    img_top_left = inv_trans.map(screen_rect.topLeft())
                    img_bottom_right = inv_trans.map(screen_rect.bottomRight())
                    image_pixel_rect = QRect(img_top_left, img_bottom_right).normalized()
                    self.drag_finished_signal.emit(screen_rect, image_pixel_rect)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(236, 236, 236))
        
        if self.original_pixmap:
            painter.setTransform(self.get_combined_transform())
            painter.drawPixmap(0, 0, self.original_pixmap)
            
            # 💡 [검수 시각화]: Qwen2.5 절대 픽셀 좌표 1:1 드로잉 (민트색 형광 박스)
            if self.highlight_box_pixel and len(self.highlight_box_pixel) == 4:
                xmin, ymin, xmax, ymax = self.highlight_box_pixel
                target_rect = QRect(QPoint(int(xmin), int(ymin)), QPoint(int(xmax), int(ymax)))
                
                fill_color = QColor(0, 255, 255, 85)     # 반투명 시안 민트
                border_color = QColor(0, 180, 255, 220)   # 파란 테두리
                
                painter.fillRect(target_rect, fill_color)
                painter.setPen(QPen(border_color, 2, Qt.PenStyle.SolidLine))
                painter.drawRect(target_rect)
                
            painter.resetTransform()
            
        if self.is_cropping and not self.begin.isNull() and not self.end.isNull():
            pen = QPen(QColor(226, 107, 10, 200), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(QRect(self.begin, self.end).normalized())


def clean_file_path(raw_path):
    if not raw_path: return ""
    while isinstance(raw_path, (tuple, list)):
        if len(raw_path) > 0: raw_path = raw_path
        else: return ""
    path_str = str(raw_path).strip()
    path_str = path_str.lstrip("([\u201c\u201d'\"").rstrip(")]\u201c\u201d'\",")
    return re.sub(r"['\"]?,?\s*$", "", path_str).strip("'\" ")


class PdfLoadWorker(QThread):
    loaded_signal = pyqtSignal(bytes)
    error_signal = pyqtSignal(str)

    def __init__(self, file_path):
        super().__init__()
        self.file_path = clean_file_path(file_path)

    def run(self):
        try:
            file_path_str = str(self.file_path)
            is_pdf = bool(re.search(r"\.pdf", file_path_str, re.IGNORECASE))
            if_pdf = is_pdf
            if if_pdf:
                pages = convert_from_path(file_path_str, dpi=200, poppler_path=POPPLER_PATH)
                if len(pages) > 0:
                    first_page = pages[0]
                    buffer = io.BytesIO()
                    first_page.save(buffer, format="JPEG")
                    self.loaded_signal.emit(buffer.getvalue())
                else:
                    raise Exception("PDF 내부에서 페이지를 찾을 수 없습니다.")
            else:
                with open(file_path_str, "rb") as f:
                    self.loaded_signal.emit(f.read())
        except Exception as e:
            self.error_signal.emit(str(e))
class OcrWorker(QThread):
    text_chunk_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, base64_image):
        super().__init__()
        self.base64_image = base64_image

    def run(self):
        payload = {
            "model": "qwen2.5-vl-7b",
            "messages": [
                {
                    "role": "system",
                    "content": "너는 도면 전문 고정밀 시각-언어(VLM) OCR 엔진이야."
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text", 
                            "text": (
                                "제공된 설계 도면 이미지에서 식별 가능한 모든 치수 숫자 데이터와 위치 구역을 전부 추출해라.\n"
                                "토큰 소모를 최소화하기 위해 반드시 아래 명시된 대괄호 압축 형식으로만 출력해라. JSON 구조나 마크다운 백틱, 부가 설명은 절대 쓰지 마라.\n\n"
                                "규칙:\n"
                                "1. 치수 숫자는 무조건 소수점 딱 1자리까지만 표시해라 (예: 550 -> 550.0).\n"
                                "2. 좌표는 [xmin, ymin, xmax, ymax] 절대 픽셀 좌표계 정수로 매핑해라.\n"
                                "3. 출력 형식: [xmin,ymin,xmax,ymax]:치수값 (중간에 띄어쓰기 없이 한 줄에 하나씩만 출력)\n\n"
                                "출력 예시:\n"
                                "[120,340,180,390]:550.0\n"
                                "[450,890,520,930]:192.0"
                            )
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{self.base64_image}"}}
                    ]
                }
            ],
            "temperature": 0.01,
            "max_tokens": 8192, # LM 스튜디오 연동 최대 마진 확보
            "stream": True  
        }
        full_response = ""
        try:
            response = requests.post(LM_STUDIO_URL, json=payload, timeout=150, stream=True)
            response.raise_for_status()
            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8').strip()
                    if "[DONE]" in decoded_line: 
                        break
                    if decoded_line.startswith("data: "):
                        data_str = decoded_line[6:].strip()
                        try:
                            data_json = json.loads(data_str)
                            if not data_json.get('choices'): 
                                continue
                            content = data_json['choices']['delta'].get('content', '')
                            if content:
                                full_response += content
                                self.text_chunk_signal.emit(content)
                        except: 
                            pass
            self.finished_signal.emit(full_response)
        except Exception as e:
            self.error_signal.emit(str(e))


class CropOcrWorker(QThread):
    crop_finished_signal = pyqtSignal(str, str)

    def __init__(self, crop_base64, box_str):
        super().__init__()
        self.crop_base64 = crop_base64  
        self.box_str = box_str

    def run(self):
        payload = {
            "model": "qwen2.5-vl-7b",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text", 
                            "text": "이 잘라낸 도면 이미지 조각 안에 적혀있는 핵심 치수 숫자가 무엇인지 숫자만 딱 대답해줘. 예: 110.0"
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{self.crop_base64}"}}
                    ]
                }
            ],
            "temperature": 0.01
        }
        try:
            response = requests.post(LM_STUDIO_URL, json=payload, timeout=30)
            result = response.json()
            ai_digit = result['choices'][0]['message']['content'].strip()
            digit_match = re.search(r"\d+\.\d+|\d+", ai_digit)
            if digit_match:
                val = float(digit_match.group(0))
                ai_digit = f"{val:.1f}"
            self.crop_finished_signal.emit(ai_digit, self.box_str)
        except Exception as e:
            self.crop_finished_signal.emit("0.0", self.box_str)
class DrawingOcrApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.selected_file_path = ""
        self.base64_img_data = ""
        self.raw_image_bytes = None
        self.qimage_source = None
        self.stream_buffer = ""  
        self.initUI()

    def initUI(self):
        self.setWindowTitle("도면 인터랙티브 고정밀 OCR 시스템 (초압축 픽셀 데이터셋 에디션)")
        self.setGeometry(50, 50, 1300, 800)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        
        left_panel = QVBoxLayout()
        self.canvas_title = QLabel("도면 시각화 뷰어 (Ctrl+휠: 확대축소 / 우클릭드래그: 화면이동 / 좌클릭드래그: 부분정밀분석)")
        self.canvas_title.setFont(QFont("맑은 고딕", 10, QFont.Weight.Bold))
        
        self.canvas = DrawingCanvas()
        self.canvas.drag_finished_signal.connect(self.handle_user_crop_drag)
        
        control_layout = QHBoxLayout()
        btn_open = QPushButton("도면 파일 가져오기")
        btn_open.setFixedHeight(35)
        btn_open.clicked.connect(self.open_file)
        
        self.btn_run = QPushButton("인공지능 도면 정밀 분석 시작")
        self.btn_run.setFixedHeight(35)
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self.run_ocr)
        
        control_layout.addWidget(btn_open)
        control_layout.addWidget(self.btn_run)
        
        self.log_viewer = QLabel("실시간 서버 분석 데이터 매핑 상태 로그창...")
        self.log_viewer.setMinimumHeight(100)
        self.log_viewer.setStyleSheet("background-color: #222222; color: #00FF00; padding: 8px;")
        self.log_viewer.setFont(QFont("Consolas", 9))
        self.log_viewer.setWordWrap(True)
        
        left_panel.addWidget(self.canvas_title)
        left_panel.addWidget(self.canvas, 1) 
        left_panel.addLayout(control_layout)
        left_panel.addWidget(self.log_viewer)
        
        right_panel = QVBoxLayout()
        table_title = QLabel("추출 및 보정 데이터 리스트 (행 클릭시 도면 위치 하이라이트)")
        table_title.setFont(QFont("맑은 고딕", 10, QFont.Weight.Bold))
        
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["최종 확인 치수값", "매핑 좌표 (xmin, ymin, xmax, ymax)", "유형"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self.on_table_selection_changed)

        table_edit_layout = QHBoxLayout()
        btn_add = QPushButton("수동 항목 행 추가")
        btn_add.clicked.connect(self.add_manual_row)
        btn_del = QPushButton("선택 행 삭제")
        btn_del.clicked.connect(self.delete_row)
        table_edit_layout.addWidget(btn_add)
        table_edit_layout.addWidget(btn_del)
        
        btn_save_excel = QPushButton("보정 완료본 엑셀 내보내기")
        btn_save_excel.setFixedHeight(40)
        btn_save_excel.setStyleSheet("background-color: #4F81BD; color: white; font-weight: bold;")
        btn_save_excel.clicked.connect(self.save_excel_file)
        
        self.btn_save_dataset = QPushButton("드래그한 보정본을 AI 피드백 학습 데이터셋으로 영구 저장")
        self.btn_save_dataset.setFixedHeight(45)
        self.btn_save_dataset.setStyleSheet("background-color: #E26B0A; color: white; font-weight: bold;")
        self.btn_save_dataset.clicked.connect(self.save_dataset_file)
        
        right_panel.addWidget(table_title)
        right_panel.addWidget(self.table, 1)
        right_panel.addLayout(table_edit_layout)
        right_panel.addWidget(btn_save_excel)
        right_panel.addWidget(self.btn_save_dataset)
        
        main_layout.addLayout(left_panel, 55)
        main_layout.addLayout(right_panel, 45)

    def open_file(self):
        import ctypes
        from ctypes import wintypes
        buffer_size = 260
        filename_buffer = ctypes.create_unicode_buffer(buffer_size)
        
        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
                ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
                ("FlagsEx", wintypes.DWORD)
            ]
            
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.hwndOwner = int(self.winId())
        ofn.lpstrFile = ctypes.cast(filename_buffer, wintypes.LPWSTR)
        ofn.nMaxFile = buffer_size
        ofn.lpstrFilter = "모든 도면 파일 (*.pdf;*.png;*.jpg)\0*.pdf;*.png;*.jpg\0모든 파일 (*.*)\0*.*\0"
        ofn.Flags = 0x00080000 | 0x00000800 
        
        comdlg32 = ctypes.windll.comdlg32
        if comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            self.selected_file_path = clean_file_path(filename_buffer.value)
            if not self.selected_file_path: return
            
            self.log_viewer.setText("도면 파일을 고해상도로 안전 변환하는 중입니다. 잠시만 기다려주세요...")
            self.btn_run.setEnabled(False)
            
            self.load_worker = PdfLoadWorker(self.selected_file_path)
            self.load_worker.loaded_signal.connect(self.on_file_loaded)
            self.load_worker.error_signal.connect(self.on_file_load_error)
            self.load_worker.start()

    def on_file_loaded(self, img_bytes):
        self.raw_image_bytes = img_bytes
        self.qimage_source = QImage.fromData(self.raw_image_bytes)
        self.canvas.set_image(self.qimage_source)
        
        file_name = os.path.basename(self.selected_file_path)
        self.canvas_title.setText(f"도면 시각화 뷰어 - {file_name}")
        self.log_viewer.setText("도면 파일 로딩 및 고해상도 변환 완료! 분석을 시작할 수 있습니다.")
        self.btn_run.setEnabled(True)

    def on_file_load_error(self, err):
        QMessageBox.critical(self, "변환 오류", f"도면 고해상도 변환에 실패했습니다:\n{err}")

    def run_ocr(self):
        if not self.raw_image_bytes: return
        self.base64_img_data = base64.b64encode(self.raw_image_bytes).decode('utf-8')
        self.log_viewer.setText("인공지능 연산 가동 중... (실시간으로 표에 치수가 누적됩니다)\n")
        self.table.setRowCount(0)
        self.stream_buffer = "" 
        
        self.worker = OcrWorker(self.base64_img_data)
        self.worker.text_chunk_signal.connect(self.append_log_and_stream_parse)
        self.worker.finished_signal.connect(self.on_ocr_finished)
        self.worker.error_signal.connect(self.handle_error)
        self.worker.start()
        self.btn_run.setEnabled(False)

    def append_log_and_stream_parse(self, text):
        # 실시간 스트리밍 버퍼 누적 및 정규식 기반 파싱 로직 구현 생략 (전체 소스코드는 기존 구현 참고)
        pass

    def on_ocr_finished(self, total_text):
        # 스트리밍 완료 후 최종 데이터 파싱 및 테이블 반영 처리 로직 구현 생략
        pass

    def handle_error(self, msg):
        QMessageBox.critical(self, "인공지능 서버 통신 에러", f"LM Studio 서버 연결에 실패했습니다.\n\n상세 내용: {msg}")
        self.btn_run.setEnabled(True)

    def add_table_row(self, value, box_str, type_str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(value))
        self.table.setItem(row, 1, QTableWidgetItem(box_str))
        self.table.setItem(row, 2, QTableWidgetItem(type_str))
        if type_str in ["인간 보정", "수동 입력"]:
            for col in range(3):
                item = self.table.item(row, col)
                if item: item.setForeground(Qt.GlobalColor.darkRed)

    def on_table_selection_changed(self):
        selected_rows = self.table.selectedItems()
        if not selected_rows:
            self.canvas.set_highlight_box(None)
            return
            
        row = self.table.currentRow()
        box_item = self.table.item(row, 1)
        if not box_item:
            self.canvas.set_highlight_box(None)
            return
            
        box_text = box_item.text().strip()
        try:
            box_pixel = json.loads(box_text)
            if isinstance(box_pixel, list) and len(box_pixel) == 4:
                self.canvas.set_highlight_box(box_pixel)
                val_item = self.table.item(row, 0)
                val_text = val_item.text() if val_item else "알 수 없음"
                self.log_viewer.setText(f"🔍 검수 모드 가동 -> 현재 선택된 치수: {val_text} | 픽셀좌표: {box_text}")
            else:
                self.canvas.set_highlight_box(None)
        except:
            self.canvas.set_highlight_box(None)

    def handle_user_crop_drag(self, screen_rect, pixel_rect):
        if self.qimage_source is None: return
        img_w = self.qimage_source.width()
        img_h = self.qimage_source.height()
        
        crop_x = max(0, min(pixel_rect.left(), img_w - 1))
        crop_y = max(0, min(pixel_rect.top(), img_h - 1))
        crop_w = max(5, min(pixel_rect.width(), img_w - crop_x))
        crop_h = max(5, min(pixel_rect.height(), img_h - crop_y))
        
        cropped_qimage = self.qimage_source.copy(crop_x, crop_y, crop_w, crop_h)
        
        q_array = QByteArray()
        q_buffer = QBuffer(q_array)
        q_buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        cropped_qimage.save(q_buffer, "JPEG")
        crop_base64 = base64.b64encode(q_array.data()).decode('utf-8')
        
        # 💡 드래그 역시 절대 픽셀 좌표 규격([xmin, ymin, xmax, ymax])으로 일치
        xmin, ymin = int(crop_x), int(crop_y)
        xmax, ymax = int(crop_x + crop_w), int(crop_y + crop_h)
        box_string = f"[{xmin}, {ymin}, {xmax}, {ymax}]"

        self.log_viewer.setText(f"드래그 영역 부분 크롭 OCR 타겟 연산 시동... 좌표: {box_string}")
        self.crop_worker = CropOcrWorker(crop_base64, box_string)
        self.crop_worker.crop_finished_signal.connect(self.append_cropped_result)
        self.crop_worker.start()

    def append_cropped_result(self, ai_read_value, box_string):
        self.add_table_row(ai_read_value, box_string, "인간 보정")
        self.log_viewer.setText(f"부분 크롭 OCR 완료! 감지값: {ai_read_value}")

    def add_manual_row(self):
        self.add_table_row("0.0", "[]", "수동 입력")

    def delete_row(self):
        row = self.table.currentRow()
        if row >= 0: self.table.removeRow(row)

    def save_excel_file(self):
        if self.table.rowCount() == 0: return
        save_path = os.path.join(BASE_DIR, "도면_치수_추출_결과.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.title = "최종 보정 치수"
        ws.append(["최종 확인 치수값", "좌표 정보"])
        for r in range(self.table.rowCount()):
            val = self.table.item(r, 0).text() if self.table.item(r, 0) else ""
            box = self.table.item(r, 1).text() if self.table.item(r, 1) else ""
            ws.append([val, box])
        try:
            wb.save(save_path)
            QMessageBox.information(self, "성공", f"최종 보정본 엑셀 파일 저장 완료:\n{save_path}")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"엑셀 저장 실패: {e}")

    def save_dataset_file(self):
        if self.table.rowCount() == 0 or not self.base64_img_data:
            QMessageBox.warning(self, "경고", "누적 저장할 유효한 데이터가 없습니다.")
            return
        dimensions_list = []
        for r in range(self.table.rowCount()):
            val = self.table.item(r, 0).text() if self.table.item(r, 0) else ""
            box = self.table.item(r, 1).text() if self.table.item(r, 1) else ""
            try: parsed_box = json.loads(box)
            except: parsed_box = []
            dimensions_list.append({"value": val, "box_2d": parsed_box})
            
        dataset_entry = {
            "image": self.selected_file_path,
            "conversations": [
                {"from": "user", "value": "설계 도면 이미지 내 치수 숫자의 정확한 값과 위치 좌표 상자를 찾아 판독해줘."},
                {"from": "assistant", "value": str(dimensions_list)}
            ]
        }
        filename = f"qwen_finetune_pair_{len(dimensions_list)}.json"
        save_path = os.path.join(DATASET_DIR, filename)
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(dataset_entry, f, ensure_ascii=False, indent=4)
            QMessageBox.information(self, "피드백 학습 완료", f"파인튜닝 학습 쌍이 저장되었습니다:\n{save_path}")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"데이터셋 누적 실패: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DrawingOcrApp()
    window.show()
    sys.exit(app.exec())
