import sys
import os
import cv2
import numpy as np
import re
import warnings
import csv
import multiprocessing
from tkinter import filedialog, Tk

# ==========================================
# 1단계: 필수 의존성 라이브러리 및 딥러닝 엔진 검증
# ==========================================
try:
    import fitz  # PyMuPDF
    import torch
    import torch.nn as nn
    import easyocr
    from easyocr.recognition import get_recognizer
    from easyocr.utils import CTCLabelConverter
    from easyocr.model.model import Model as EasyOCRModel
except ImportError as e:
    print("Error: Missing library. Please run 'pip install pymupdf easyocr torch'.")
    input("Press Enter to exit...")
    sys.exit(-1)

def show_exception_and_exit(exc_type, exc_value, tb):
    import traceback
    print("\n" + "="*50)
    print("Program stopped due to an error.")
    print("="*50)
    traceback.print_exception(exc_type, exc_value, tb)
    print("="*50)
    input("Press Enter to exit...")
    sys.exit(-1)

sys.excepthook = show_exception_and_exit
warnings.filterwarnings("ignore", category=UserWarning)
os.environ["TORCH_CPP_LOG_LEVEL"] = "ERROR"

# PyInstaller 임시 폴더 경로 인식 함수 (전역 배치)
def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# 윈도우 환경 멀티프로세싱 크래시 방지 구동
multiprocessing.freeze_support()

# ==========================================
# 2단계: 파일 탐색기를 통한 도면 선택
# ==========================================
root = Tk()
root.withdraw()
root.attributes('-topmost', True)  # 파일 선택창을 화면 맨 위로 강제 호출
print("Select your drawing file (Image or PDF)...")
file_path = filedialog.askopenfilename(
    title="Select Drawing File",
    filetypes=[
        ("All Supported Files", "*.png;*.jpg;*.jpeg;*.bmp;*.tiff;*.gif;*.pdf"),
        ("Image Files", "*.png;*.jpg;*.jpeg;*.bmp;*.tiff;*.gif"),
        ("PDF Files", "*.pdf"),
        ("All Files", "*.*")
    ]
)

if not file_path:
    print("File selection canceled. Program terminated.")
    sys.exit()

if isinstance(file_path, tuple) and len(file_path) > 0:
    file_path = file_path[0]

print("Selected file: " + os.path.basename(file_path))
root.destroy()

# ==========================================
# 3단계: 고해상도 이미지 및 PDF 변환 로드
# ==========================================
image = None
_, ext_check = os.path.splitext(file_path.lower())

if ext_check == '.pdf':
    try:
        print("Converting PDF to high-quality image...")
        doc = fitz.open(file_path)
        page = doc[0]  
        zoom = 4.0  
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)
        image = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
        doc.close()
    except Exception as e:
        print("Error reading PDF:", e)
        image = None
else:
    try:
        img_array = np.fromfile(file_path, np.uint8)
        image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    except Exception as e:
        image = None

if image is None:
    print("Failed to load image. Please check the file.")
    sys.exit()

h, w, _ = image.shape

# ==========================================
# 4단계: 고급 마우스 휠 줌 및 패닝 윈도우 가동
# ==========================================
cv2.namedWindow("Drawing Grid", cv2.WINDOW_AUTOSIZE)

zoom_factor = min(1024.0 / w, 768.0 / h)
pan_x = (1024 - w * zoom_factor) / 2
pan_y = (768 - h * zoom_factor) / 2
is_dragging = False
start_x, start_y = 0, 0

x_lines = []
y_lines = []
current_mode = "vertical"  

def redraw_window():
    global zoom_factor, pan_x, pan_y, current_mode, x_lines, y_lines
    canvas = image.copy()
    for x in x_lines:
        cv2.line(canvas, (x, 0), (x, h), (255, 0, 0), 2)
    for y in y_lines:
        cv2.line(canvas, (0, y), (w, y), (0, 0, 255), 2)

    M = np.float32([[zoom_factor, 0, pan_x], [0, zoom_factor, pan_y]])
    display_img = cv2.warpAffine(canvas, M, (1024, 768))
    mode_text = f"Mode: {current_mode.upper()} | L-Click: Add | R-Click: Undo Line | Enter: Next"
    cv2.putText(display_img, mode_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2, cv2.LINE_AA)
    cv2.imshow("Drawing Grid", display_img)

def mouse_callback(event, x, y, flags, param):
    global zoom_factor, pan_x, pan_y, is_dragging, start_x, start_y, current_mode, x_lines, y_lines
    orig_x = int((x - pan_x) / zoom_factor)
    orig_y = int((y - pan_y) / zoom_factor)

    if event == cv2.EVENT_MBUTTONDOWN:
        is_dragging = True
        start_x, start_y = x, y
    elif event == cv2.EVENT_MOUSEMOVE:
        if is_dragging:
            pan_x += x - start_x
            pan_y += y - start_y
            start_x, start_y = x, y
            redraw_window()
    elif event == cv2.EVENT_MBUTTONUP:
        is_dragging = False
    elif event == cv2.EVENT_RBUTTONDOWN:
        if current_mode == "vertical" and x_lines:
            x_lines.pop(); redraw_window()
        elif current_mode == "horizontal" and y_lines:
            y_lines.pop(); redraw_window()
    elif event == cv2.EVENT_MOUSEWHEEL:
        old_zoom = zoom_factor
        zoom_factor *= 1.15 if flags > 0 else (1.0 / 1.15)
        zoom_factor = max(0.05, min(zoom_factor, 50.0))
        pan_x = x - (x - pan_x) * (zoom_factor / old_zoom)
        pan_y = y - (y - pan_y) * (zoom_factor / old_zoom)
        redraw_window()
    elif event == cv2.EVENT_LBUTTONDOWN:
        if 0 <= orig_x <= w and 0 <= orig_y <= h:
            if current_mode == "vertical":
                x_lines.append(orig_x); x_lines.sort()
            elif current_mode == "horizontal":
                y_lines.append(orig_y); y_lines.sort()
            redraw_window()

cv2.setMouseCallback("Drawing Grid", mouse_callback)

print("Mode: VERTICAL. Press Enter when done.")
while True:
    redraw_window()
    key = cv2.waitKey(1) & 0xFF
    if key == 13 or key == 10:  
        if current_mode == "vertical":
            current_mode = "horizontal"
            print("Mode: HORIZONTAL. Press Enter when done.")
        elif current_mode == "horizontal":
            current_mode = "done"
            break
    elif key == 27:  
        print("Canceled.")
        cv2.destroyAllWindows()
        sys.exit()

cv2.destroyAllWindows()
import traceback

print("\n" + "="*50)
try:
    max_num_input = input("이 도면의 최대 일련번호(넘버링 끝 숫자)를 입력하세요 (예: 120): ").strip()
    MAX_ALLOWED_NUMBER = int(max_num_input)
    print(f"-> 1부터 {MAX_ALLOWED_NUMBER} 범위 내의 숫자만 유효 데이터로 정밀 필터링합니다.")
except ValueError:
    MAX_ALLOWED_NUMBER = 999  
    print("-> 올바른 숫자가 입력되지 않아 기본 제한(999)으로 진행합니다.")
print("="*50 + "\n")

# [보정 완료]: 구석 0 좌표를 강제로 배열에 묶어주어 도면 테두리 끝단 그리드 활성화
x_bounds = sorted(list(set([0] + x_lines + [w])))
y_bounds = sorted(list(set([0] + y_lines + [h])))
num_cols, num_rows = len(x_bounds) - 1, len(y_bounds) - 1

row_labels = ["F", "E", "D", "C", "B", "A"]
rows = row_labels[-num_rows:] if num_rows <= len(row_labels) else [chr(65 + num_rows - 1 - i) for i in range(num_rows)]
cols = [str(i + 1) for i in range(num_cols)]

scale_factor = 2.0
img_resized = cv2.resize(image, (None, None), fx=scale_factor, fy=scale_factor, interpolation=cv2.INTER_CUBIC)
white_background = np.ones_like(img_resized) * 255

# [수정 완료]: 600 DPI 고해상도 도면에 맞춰 스탬프 원 마크를 강력하게 격리하는 대형 커널로 스케일업
kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

MAX_STAGES = 30
hsv_thresholds = []
for i in range(MAX_STAGES):
    val = int(20 + (145 / (MAX_STAGES - 1)) * i)
    hsv_thresholds.append({"s_min": min(val, 165), "v_min": min(val, 165)})

only_red_stages = {}

current_img = img_resized.copy()
hsv_1st = cv2.cvtColor(current_img, cv2.COLOR_BGR2HSV)
mask1 = cv2.inRange(hsv_1st, np.array([0, hsv_thresholds[0]["s_min"], hsv_thresholds[0]["v_min"]]), np.array([10, 255, 255]))
mask2 = cv2.inRange(hsv_1st, np.array([160, hsv_thresholds[0]["s_min"], hsv_thresholds[0]["v_min"]]), np.array([180, 255, 255]))
red_mask_1st = mask1 + mask2
morph_1st = cv2.morphologyEx(red_mask_1st, cv2.MORPH_CLOSE, kernel)
only_red_stages[1] = np.where(morph_1st[:, :, np.newaxis] > 127, current_img, white_background)

for stage in range(1, MAX_STAGES + 1):
    prev_img = only_red_stages[stage - 1].copy()
    hsv_stage = cv2.cvtColor(prev_img, cv2.COLOR_BGR2HSV)
    
    s_val = hsv_thresholds[stage - 1]["s_min"]
    v_val = hsv_thresholds[stage - 1]["v_min"]
    
    m1 = cv2.inRange(hsv_stage, np.array([0, s_val, v_val]), np.array([10, 255, 255]))
    m2 = cv2.inRange(hsv_stage, np.array([160, s_val, v_val]), np.array([180, 255, 255]))
    stage_mask = m1 + m2
    
    morph_stage = cv2.morphologyEx(stage_mask, cv2.MORPH_CLOSE, kernel)
    only_red_stages[stage] = np.where(morph_stage[:, :, np.newaxis] > 127, prev_img, white_background)

cross_validated_raw_data = {}
successfully_detected_numbers = set()

# 디버깅용 텍스트 보고서 파일 생성 초기화
base_dir = os.path.dirname(file_path)
file_name_only, _ = os.path.splitext(os.path.basename(file_path))
txt_log_path = os.path.join(base_dir, f"{file_name_only}_ocr_debug_log.txt")

with open(txt_log_path, "w", encoding="utf-8") as log_f:
    log_f.write("==================================================\n")
    log_f.write(f" OCR AI Misrecognition Error Audit Report\n")
    log_f.write(f" Target Drawing: {os.path.basename(file_path)}\n")
    log_f.write(" [성공 제거 / 실패 및 오인식 집중 추적 가동 (문턱값 0.05 완화본)]\n")
    log_f.write("==================================================\n\n")
try:
    print("Initializing Dual Hybrid OCR Engine (Pure + Custom ResNet)...")
    import torch
    import torch.nn as nn
    from easyocr.model.model import Model as EasyOCRModel
    from easyocr.utils import CTCLabelConverter

    reader_pure = easyocr.Reader(['en'], gpu=True) 

    custom_model_dir = get_resource_path("custom_models")
    reader_custom = easyocr.Reader(['en'], gpu=True, model_storage_directory=custom_model_dir)
    digit_chars = '0123456789'
    reader_custom.character = digit_chars
    reader_custom.converter = CTCLabelConverter(digit_chars)

    custom_recognizer = EasyOCRModel(1, 512, 256, 11)
    
    pth_absolute_path = os.path.join(custom_model_dir, "ko_g2.pth")
    if not os.path.exists(pth_absolute_path):
        pth_absolute_path = r"C:\Users\win10\.EasyOCR\model\custom_drawing_easyocr_ResNet.pth"
        
    state_dict = torch.load(pth_absolute_path, map_location='cuda' if torch.cuda.is_available() else 'cpu', weights_only=False)
    
    new_state_dict = {}
    for key, value in state_dict.items():
        if key.startswith("module."):
            new_key = key
        else:
            new_key = f"module.{key}"
        new_state_dict[new_key] = value
        
    custom_recognizer = nn.DataParallel(custom_recognizer)
    if torch.cuda.is_available():
        custom_recognizer = custom_recognizer.cuda()
    custom_recognizer.load_state_dict(new_state_dict, strict=True)
    
    if hasattr(reader_custom, 'recognizer'):
        reader_custom.recognizer = custom_recognizer
        print("Success: Pure ResNet Weights injected into Second Hybrid pipeline.")

    target_stages = list(range(1, MAX_STAGES + 1))
    print(f"Starting Multi-Stage Adaptive OCR pipeline (Stages: {target_stages})...")
    full_set = set(range(1, MAX_ALLOWED_NUMBER + 1))
    
    # [메모리 최적화]: 오려낸 가벼운 낱개 사진 파편만 보관하는 보관소
    all_stage_crop_registry = []
    
    for stage_num in target_stages:
        stage_fail_buffer = []

        stage_hsv = cv2.cvtColor(only_red_stages[stage_num], cv2.COLOR_BGR2HSV)
        s_val = hsv_thresholds[stage_num - 1]["s_min"]
        v_val = hsv_thresholds[stage_num - 1]["v_min"]
        
        # [오류 완전 정정]: 누락되었던 상한선 배열값 [10, 255, 255]와 [180, 255, 255]를 정확히 삽입했습니다.
        sm1 = cv2.inRange(stage_hsv, np.array([0, s_val, v_val]), np.array([10, 255, 255]))
        sm2 = cv2.inRange(stage_hsv, np.array([160, s_val, v_val]), np.array([180, 255, 255]))
        stage_red_mask = sm1 + sm2
        stage_morph = cv2.morphologyEx(stage_red_mask, cv2.MORPH_CLOSE, kernel)
        ocr_source_img = cv2.bitwise_not(stage_morph)
        img_h, img_w = ocr_source_img.shape[:2]
        
        results_pure = reader_pure.readtext(
            ocr_source_img, 
            batch_size=1,
            text_threshold=0.03, link_threshold=0.15, low_text=0.02,
            slope_ths=0.3, ycenter_ths=0.4, height_ths=0.8, width_ths=0.9
        )
        
        results_custom = reader_custom.readtext(
            ocr_source_img, 
            batch_size=1,
            text_threshold=0.03, link_threshold=0.15, low_text=0.02,
            slope_ths=0.3, ycenter_ths=0.4, height_ths=0.8, width_ths=0.9
        )
        
        combined_results = []
        for r in results_pure: combined_results.append((r, "Pure"))
        for r_c in results_custom: combined_results.append((r_c, "Custom"))
            
        # 1. 신뢰도(prob) 점수가 높은 순서대로 정렬
        combined_results.sort(key=lambda x: x[0][2], reverse=True)

        accepted_results = []
        for (bbox, text, prob), source in combined_results:
            pts = np.array(bbox, dtype=np.int32)
            bx, by, bw, bh = cv2.boundingRect(pts)
            current_center_x = bx + bw / 2.0
            current_center_y = by + bh / 2.0

            # 2. 이미 채택된 고신뢰도 결과의 좌표와 비교하여 중복 여부 체크
            is_duplicate = False
            for (a_bbox, a_text, a_prob), a_source in accepted_results:
                a_pts = np.array(a_bbox, dtype=np.int32)
                abx, aby, abw, abh = cv2.boundingRect(a_pts)
                assigned_center_x = abx + abw / 2.0
                assigned_center_y = aby + abh / 2.0

                # 두 글자의 중심점 거리가 15픽셀 이내라면 동일한 위치의 스탬프로 판단
                distance = np.sqrt((current_center_x - assigned_center_x)**2 + (current_center_y - assigned_center_y)**2)
                if distance < 15:  # 도면 해상도에 따라 10~25 사이로 조절 가능
                    is_duplicate = True
                    break

            # 3. 중복되지 않은 가장 정답에 가까운(prob가 높은) 결과만 최종 루프에 진입시킴
            if not is_duplicate:
                accepted_results.append(((bbox, text, prob), source))

        for (bbox, text, prob), source in combined_results:
            raw_text = str(text).upper().strip()
            
            # [메모리 절약]: 스탬프 구역만큼만 오려냅니다.
            pts = np.array(bbox, dtype=np.int32)
            x1 = int(max(0, np.min(pts[:, 0])))
            x2 = int(min(img_w, np.max(pts[:, 0])))
            y1 = int(max(0, np.min(pts[:, 1])))
            y2 = int(min(img_h, np.max(pts[:, 1])))
            
            crop_img = None
            if (x2 - x1) > 0 and (y2 - y1) > 0:
                crop_img = ocr_source_img[y1:y2, x1:x2].copy()
            
            if prob < 0.15:
                if crop_img is not None:
                    all_stage_crop_registry.append((crop_img, bbox, raw_text, prob))
                stage_fail_buffer.append(f"  [실패] {source} -> 원래텍스트: '{raw_text}' | 원인: 점수 미달 (Prob: {prob:.2f})")
                continue

            replacements = {
                'I': '1', 'L': '1', 'O': '0', 'Z': '2', 'S': '5', 'T': '7', 'B': '8', 'G': '9'
            }
            fixed_text = ""
            for char in raw_text:
                fixed_text += replacements.get(char, char)

            extracted_numbers = re.findall(r'\d+', fixed_text)
            
            if not extracted_numbers:
                if crop_img is not None:
                    all_stage_crop_registry.append((crop_img, bbox, raw_text, prob))
                stage_fail_buffer.append(f"  [실패] {source} -> 원래텍스트: '{raw_text}' | 원인: 내부 숫자 분할 정제 실패")
                continue

            for num_str in extracted_numbers:
                try:
                    val_int = int(num_str)
                    if not (1 <= val_int <= MAX_ALLOWED_NUMBER):
                        if crop_img is not None:
                            all_stage_crop_registry.append((crop_img, bbox, raw_text, prob))
                        stage_fail_buffer.append(f"  [실패] {source} -> 원래텍스트: '{raw_text}' (추출조각: {val_int}) | 원인: 일련번호 허용 범위 초과")
                        continue
                    clean_text = str(val_int)
                except ValueError:
                    if crop_img is not None:
                        all_stage_crop_registry.append((crop_img, bbox, raw_text, prob))
                    stage_fail_buffer.append(f"  [실패] {source} -> 추출조각문자: '{num_str}' | 원인: 수치 형식 변환 실패")
                    continue

                bx, by, bw, bh = cv2.boundingRect(pts)
                center_x = int((bx + bw / 2.0) / scale_factor)
                center_y = int((by + bh / 2.0) / scale_factor)

                center_x = max(0, min(w - 1, center_x))
                center_y = max(0, min(h - 1, center_y))

                assigned_row, assigned_col = None, None
                EPSILON = 2  # 픽셀 단위 여유 마진

                
                for r_idx in range(num_rows):
                    if y_bounds[r_idx] <= center_y <= y_bounds[r_idx+1]:
                        assigned_row = rows[r_idx]
                        break
                for c_idx in range(num_cols):
                    if x_bounds[c_idx] <= center_x <= x_bounds[c_idx+1]:
                        assigned_col = cols[c_idx]
                        break
                        
                if assigned_row is not None and assigned_col is not None:
                    grid_location = f"{assigned_row}{assigned_col}"
                    if grid_location not in cross_validated_raw_data:
                        cross_validated_raw_data[grid_location] = set()
                    cross_validated_raw_data[grid_location].add(clean_text)
                    successfully_detected_numbers.add(val_int)
                    if crop_img is not None:
                        all_stage_crop_registry.append((crop_img, bbox, raw_text, prob))
                    print(f"  [성공] {source} -> 격자 안착 완료: {grid_location} | 최종숫자: {val_int}")
                else:
                    if crop_img is not None:
                        all_stage_crop_registry.append((crop_img, bbox, raw_text, prob))
                    stage_fail_buffer.append(f"  [실패] {source} -> 추출숫자: {val_int} | 원래텍스트: '{raw_text}' | 원인: 구역 그리드 좌표 이탈 (Center: {center_x}, {center_y})")

        if stage_fail_buffer:
            stage_title = f"\n--- [Analyzing Only-Red Stage {stage_num} / {MAX_STAGES}] ---"
            print(stage_title)
            with open(txt_log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"{stage_title}\n")
                for fail_log in stage_fail_buffer:
                    print(fail_log)
                    log_f.write(f"{fail_log}\n")

        current_missing = full_set - successfully_detected_numbers
        stage_end_msg = f"-> Stage {stage_num} 완료: 현재 누락된 번호 개수 = {len(current_missing)}개"
        print(stage_end_msg)
        with open(txt_log_path, "a", encoding="utf-8") as log_f:
            log_f.write(f"{stage_end_msg}\n")
        
        if len(current_missing) == 0:
            print("-> 모든 일련번호가 찾아져 연산을 조기 종료합니다.")
            break

    cv2.destroyAllWindows()

    missing_numbers = sorted(list(full_set - successfully_detected_numbers))

    num_loc_counts = {}
    for loc, num_set in cross_validated_raw_data.items():
        for num in num_set:
            if num not in num_loc_counts:
                num_loc_counts[num] = {}
            num_loc_counts[num][loc] = num_loc_counts[num].get(loc, 0) + 1

    detected_numbers_summary = []
    for num, loc_dict in num_loc_counts.items():
        best_loc = max(loc_dict.keys(), key=lambda k: loc_dict[k])
        detected_numbers_summary.append((num, best_loc))

    # [수정 완료]: 인덱스 슬라이싱 x[0]과 x[1]을 정교하게 명시하여 무조건 에러가 발생하지 않도록 패스 처리함
    detected_numbers_summary.sort(key=lambda x: (int(x[0]) if x[0].isdigit() else 999, x[1]))

    sys.modules[__name__].missing_numbers_global = missing_numbers
    sys.modules[__name__].total_stages_run = stage_num

except Exception as ocr_error:
    print("\n" + "!"*60)
    print("CRITICAL CRASH DETECTED DURING OCR PROCESSING")
    print("!"*60)
    traceback.print_exc()
    input("Press Enter to close the program...")
    sys.exit(-1)
print(" 도면 내 일련번호 누락 검증 결과")
print("!"*50)

with open(txt_log_path, "a", encoding="utf-8") as log_f:
    log_f.write("\n" + "!"*50 + "\n")
    log_f.write(" Final Drawing OCR Audit Result\n")
    log_f.write("!"*50 + "\n")
    
    if missing_numbers:
        warn_msg1 = f"-> [경고] {len(missing_numbers)}개의 번호가 도면에서 누락(미인식)되었습니다."
        warn_msg2 = f"-> 누락된 번호 목록: {missing_numbers}"
        print(warn_msg1)
        print(warn_msg2)
        log_f.write(f"{warn_msg1}\n{warn_msg2}\n")
    else:
        ok_msg = "-> [완벽] 1번부터 끝 번호까지 누락된 일련번호가 전혀 없습니다!"
        print(ok_msg)
        log_f.write(f"{ok_msg}\n")
        
    log_f.write("!"*50 + "\n")
print("!"*50 + "\n")

print(f"OCR maximum value filtering completed successfully.\nLog file saved at: {txt_log_path}")

# ==========================================
# [파인튜닝 데이터셋 수집 시스템]: 초경량 파편 파일 물리 저장소 격리 보관
# ==========================================
try:
    dataset_success_dir = os.path.join(base_dir, f"{file_name_only}_dataset_success")
    dataset_fail_dir = os.path.join(base_dir, f"{file_name_only}_dataset_fail")
    os.makedirs(dataset_success_dir, exist_ok=True)
    os.makedirs(dataset_fail_dir, exist_ok=True)

    print("\nExtracting Fine-tuning Dataset images from all stages...")
    
    if 'all_stage_crop_registry' in locals() and all_stage_crop_registry:
        save_counter = 0
        # 3부에서 미리 초경량으로 다 오려둔 crop_img 알맹이를 안전하게 다이렉트로 다운로드 처리
        for crop_img, bbox, raw_text, prob in all_stage_crop_registry:
            replacements = {'I': '1', 'L': '1', 'O': '0', 'Z': '2', 'S': '5', 'T': '7', 'B': '8', 'G': '9'}
            fixed_text = "".join([replacements.get(char, char) for char in raw_text])
            extracted_numbers = re.findall(r'\d+', fixed_text)
            
            if prob >= 0.15 and extracted_numbers:
                for num_str in extracted_numbers:
                    val_int = int(num_str)
                    if 1 <= val_int <= MAX_ALLOWED_NUMBER:
                        save_name = os.path.join(dataset_success_dir, f"{val_int}_인식성공_{save_counter}.png")
                        cv2.imencode('.png', crop_img).tofile(save_name)
                        save_counter += 1
            else:
                clean_filename = re.sub(r'[\/:*?"<>|]', '', raw_text)
                if not clean_filename: clean_filename = "unknown"
                save_name = os.path.join(dataset_fail_dir, f"{clean_filename}_인식실패_{save_counter}.png")
                cv2.imencode('.png', crop_img).tofile(save_name)
                save_counter += 1

        print(f"-> Success Dataset saved successfully at: {dataset_success_dir}")
        print(f"-> Fail Dataset saved successfully at: {dataset_fail_dir}")
    else:
        print("-> [안내] 저장할 데이터셋 메모리 맵 레지스트리가 비어있습니다.")
        
except Exception as dataset_error:
    print("\n[수집 엔진 내부 에러 발생]:")
    traceback.print_exc()

# ==========================================
# 5단계: 이미지 마크업 오버레이 및 시각화 저장
# ==========================================
output_image = image.copy()
box_overlay = output_image.copy()

for r_idx in range(num_rows):
    for c_idx in range(num_cols):
        x_min, x_max = int(x_bounds[c_idx]), int(x_bounds[c_idx+1])
        y_min, y_max = int(y_bounds[r_idx]), int(y_bounds[r_idx+1])
        label_text = f"{rows[r_idx]}{cols[c_idx]}"
        
        font_scale = max(0.6, h / 2500.0) 
        thickness = max(1, int(font_scale * 1.2))
        (text_w, text_h), _ = cv2.getTextSize(label_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        
        center_x, center_y = int((x_min + x_max) / 2), int((y_min + y_max) / 2)
        cv2.rectangle(box_overlay, (center_x - int(text_w / 2) - 5, center_y - int(text_h / 2) - 5), (center_x + int(text_w / 2) + 5, center_y + int(text_h / 2) + 5), (255, 255, 255), -1)
        cv2.rectangle(box_overlay, (center_x - int(text_w / 2) - 5, center_y - int(text_h / 2) - 5), (center_x + int(text_w / 2) + 5, center_y + int(text_h / 2) + 5), (0, 0, 0), 1)

cv2.addWeighted(box_overlay, 0.7, output_image, 0.3, 0, output_image)

for r_idx in range(num_rows):
    for c_idx in range(num_cols):
        x_min, x_max = int(x_bounds[c_idx]), int(x_bounds[c_idx+1])
        y_min, y_max = int(y_bounds[r_idx]), int(y_bounds[r_idx+1])
        label_text = f"{rows[r_idx]}{cols[c_idx]}"
        
        font_scale = max(0.6, h / 2500.0) 
        thickness = max(1, int(font_scale * 1.2))
        (text_w, text_h), _ = cv2.getTextSize(label_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        cv2.putText(output_image, label_text, (int((x_min + x_max) / 2) - int(text_w / 2), int((y_min + y_max) / 2) + int(text_h / 2)), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)
        cv2.rectangle(output_image, (x_min, y_min), (x_max, y_max), (100, 255, 100), 3) 
        
        for stage_idx in range(1, 31):
            if stage_idx in only_red_stages:
                cv2.rectangle(only_red_stages[stage_idx], (int(x_min * 2.0), int(y_min * 2.0)), (int(x_max * 2.0), int(y_max * 2.0)), (100, 255, 100), 5)

if ext_check.lower() == '.pdf': 
    ext_check = '.png'

output_img_path = os.path.join(base_dir, f"{file_name_only}_grid_result{ext_check}")
success1, im_buf1 = cv2.imencode(ext_check, output_image)
if success1:
    im_buf1.tofile(output_img_path)
    print(f"Standard grid image saved to: {output_img_path}")

for stage_idx in range(1, 31):
    if stage_idx in only_red_stages:
        suffix = f"_only red {stage_idx}" if stage_idx > 1 else "_only red"
        stage_out_path = os.path.join(base_dir, f"{file_name_only}{suffix}{ext_check}")
        success_stage, im_buf_stage = cv2.imencode(ext_check, only_red_stages[stage_idx])
        if success_stage:
            im_buf_stage.tofile(stage_out_path)
            print(f"Only-Red stage {stage_idx} image saved to: {stage_out_path}")

# 최종 CSV 보고서 작성 (UTF-8-SIG 적용)
csv_report_path = os.path.join(base_dir, f"{file_name_only}_report.csv")
with open(csv_report_path, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["Number", "Location"])
    for num, loc in detected_numbers_summary:
        writer.writerow([num, loc])
    
    missing_list = getattr(sys.modules[__name__], 'missing_numbers_global', [])
    if missing_list:
        writer.writerow([])
        writer.writerow(["[MISSING NUMBERS CONTROL]"])
        writer.writerow(["Total Missing Count", len(missing_list)])
        writer.writerow(["Missing Numbers List", str(missing_list)])

print(f"\nReport saved to: {csv_report_path}")
input("Process finished. Press Enter to exit...")
