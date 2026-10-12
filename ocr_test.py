import base64
import requests
import json
import os
import io
import sys
import re
from tkinter import Tk, filedialog
from pdf2image import convert_from_path
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

# ==============================================================================
# [설정 파일 및 경로 입력]
# ==============================================================================
# LM Studio 로컬 서버 API 주소
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"

# 프로젝트 폴더 내 poppler 내부의 bin 폴더 경로를 자동 계산합니다.
BASE_DIR = os.getcwd()
POPPLER_PATH = os.path.join(BASE_DIR, "poppler", "Library", "bin")


# ==============================================================================
# [기능 함수 정의]
# ==============================================================================
def clean_file_path(raw_path):
    """어떤 종류의 다중 튜플이나 괄호 찌꺼기가 들어와도 순수 문자열 경로만 강제로 뜯어내는 함수"""
    if not raw_path:
        return ""
    
    # 💡 무한 루프 형태로 튜플이나 리스트 알맹이가 안 나올 때까지 완전히 벗겨냅니다.
    while isinstance(raw_path, (tuple, list)):
        if len(raw_path) > 0:
            raw_path = raw_path[0]
        else:
            return ""
            
    # 문자열로 변환 후 윈도우 경로에 묻은 가짜 괄호 및 쉼표 문자를 정규식으로 완전히 청소합니다.
    path_str = str(raw_path).strip()
    path_str = path_str.lstrip("([\u201c\u201d'\"").rstrip(")]\u201c\u201d'\",")
    
    # 혹시 튜플 문자열 잔여물이 주소 뒤에 붙어있다면 잘라냅니다 (예: .pdf', 나 .pdf",)
    path_str = re.sub(r"['\"]?,?\s*$", "", path_str)
    path_str = path_str.strip("'\" ")
    
    return path_str


def select_file():
    """윈도우 파일 탐색기를 열어 이미지 또는 PDF 파일을 선택하는 함수"""
    root = Tk()
    root.withdraw() 
    root.attributes('-topmost', True) 
    
    file_types = [
        ("도면 파일 (이미지, PDF)", "*.png *.jpg *.jpeg *.bmp *.webp *.pdf"),
        ("모든 파일", "*.*")
    ]
    
    print("분석할 도면 파일(이미지 또는 PDF)을 탐색창에서 선택해주세요...")
    file_path = filedialog.askopenfilename(
        title="분석할 도면 파일 선택",
        filetypes=file_types
    )
    
    return clean_file_path(file_path)


def process_file_to_base64_list(file_path):
    """파일 형식을 확인하고 Base64 이미지 리스트로 반환하는 함수"""
    # 💡 2중 방어로 튜플 구조 파괴 함수를 한 번 더 거칩니다.
    file_path_str = clean_file_path(file_path)
    
    if not file_path_str or file_path_str == "None":
        print("[오류] 유효한 파일 경로를 식별할 수 없습니다.")
        return None
        
    # 이제 완벽한 문자열 상태이므로 .lower() 가 100% 정상 작동합니다.
    ext = os.path.splitext(file_path_str)[1].lower()
    base64_images = []

    if ext == '.pdf':
        print("PDF 파일을 이미지로 변환하는 중입니다...")
        try:
            pages = convert_from_path(file_path_str, dpi=200, poppler_path=POPPLER_PATH)
            
            for i, page in enumerate(pages):
                buffer = io.BytesIO()
                page.save(buffer, format="JPEG")
                img_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
                base64_images.append((i + 1, img_str))
            print(f"변환 완료: 총 {len(base64_images)}개의 페이지를 추출했습니다.")
        except Exception as e:
            print(f"PDF 변환 중 오류 발생: {e}")
            return None
    else:
        print("이미지 파일을 로드하는 중입니다...")
        try:
            with open(file_path_str, "rb") as image_file:
                img_str = base64.b64encode(image_file.read()).decode('utf-8')
                base64_images.append((1, img_str))
        except Exception as e:
            print(f"이미지 로드 중 오류 발생: {e}")
            return None

    return base64_images


def analyze_drawing_stream(base64_image, page_num, total_pages):
    """Qwen2.5-VL 모델에 이미지를 보내 실시간(Stream)으로 치수를 추출하는 함수"""
    payload = {
        "model": "qwen2.5-vl-7b",
        "messages": [
            {
                "role": "system",
                "content": "너는 기계 설계 도면 전문 OCR 엔진이야. 도면 내 선이나 도형 기호는 모두 무시하고, 숫자 치수 데이터 추출에만 집중해줘."
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text", 
                        "text": (
                            "제공된 설계 도면 이미지에서 선이나 도형 기호는 제외하고, 오직 '치수(Dimensions)' 및 관련 측정 수치값들만 추출해줘.\n\n"
                            "### 추출 규칙:\n"
                            "1. 이 도면의 모든 치수는 무조건 소수점 첫째 자리 형태(예: 550.0, 192.0)로 표기되어 있어.\n"
                            "2. 소수점이 없는 순수 정수 형태로 보이면 뒤에 반드시 .0을 붙여서 추출해줘. (예: M24 -> M24.0)\n\n"
                            "### 출력 형태:\n"
                            "- 메인 부품 치수 (전체 길이, 폭, 간격 등):\n"
                            "- 상세도 A 치수:\n"
                            "- 상세도 B 치수:\n"
                            "- 반복 요소 및 R값(반지름) 치수:\n"
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    }
                ]
            }
        ],
        "temperature": 0.1,  
        "stream": True  
    }

    full_response = ""
    print(f"\n[도면 치수 OCR 분석 결과 - {page_num}페이지]")
    print("=" * 60)
    
    try:
        response = requests.post(LM_STUDIO_URL, json=payload, timeout=120, stream=True)
        response.raise_for_status()
        
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith("data: "):
                    data_str = decoded_line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        data_json = json.loads(data_str)
                        delta = data_json['choices'][0]['delta']
                        if 'content' in delta:
                            content = delta['content']
                            full_response += content
                            sys.stdout.write(content)
                            sys.stdout.flush()
                    except:
                        pass
        print("\n" + "=" * 60)
        return full_response

    except requests.exceptions.RequestException as e:
        print(f"\n[오류] [{page_num}페이지] 서버 통신 에러 발생: {e}")
        return ""


def save_to_excel(text_content, excel_path):
    """추출된 텍스트 리포트를 정규식으로 파싱하여 엑셀 셀에 나누어 저장하는 함수"""
    wb = Workbook()
    ws = wb.active
    ws.title = "도면 치수 데이터"
    
    font_header = Font(name="맑은 고딕", size=11, bold=True, color="FFFFFF")
    font_body = Font(name="맑은 고딕", size=10)
    fill_header = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    
    headers = ["페이지", "분류 카테고리", "추출 데이터 및 수치"]
    ws.append(headers)
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        
    current_page = "1페이지"
    current_category = "기타 수치"
    
    lines = text_content.split('\n')
    row_num = 2
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if "[ " in line and "페이지 결과]" in line:
            current_page = line.replace("[", "").replace(" 결과]", "").strip()
            continue
            
        if line.startswith("- **") or line.startswith("**") or line.startswith("###") or line.startswith("- "):
            clean_cat = line.replace("-", "").replace("*", "").replace("#", "").strip()
            if clean_cat:
                current_category = clean_cat
            continue
            
        if any(char.isdigit() for char in line):
            clean_data = line.replace("-", "").replace("•", "").strip()
            
            ws.cell(row=row_num, column=1, value=current_page).alignment = align_center
            ws.cell(row=row_num, column=2, value=current_category).alignment = align_left
            ws.cell(row=row_num, column=3, value=clean_data).alignment = align_left
            
            for col in range(1, 4):
                ws.cell(row=row_num, column=col).font = font_body
                
            row_num += 1

    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 30
    ws.column_dimensions['C'].width = 40
        
    wb.save(excel_path)


# ==============================================================================
# [메인 실행 프로세스]
# ==============================================================================
def main():
    file_path = select_file()
    if not file_path:
        print("[안내] 파일 선택이 취소되었습니다. 프로그램을 종료합니다.")
        return
    
    file_name = os.path.basename(file_path)
    print(f"선택된 파일: {file_name}")

    image_list = process_file_to_base64_list(file_path)
    if not image_list:
        return

    total_pages = len(image_list)
    print("분석을 시작합니다. 글자가 한 자씩 실시간으로 화면에 출력됩니다...")
    
    final_output = f"=== 도면 치수 추출 결과 보고서 ===\n원본 파일명: {file_name}\n\n"
    
    for page_num, base64_image in image_list:
        ocr_result = analyze_drawing_stream(base64_image, page_num, total_pages)
        
        final_output += f"[{page_num} 페이지 결과]\n"
        final_output += ocr_result + "\n\n"
        final_output += "-" * 50 + "\n"

    txt_save_path = os.path.join(BASE_DIR, "도면_치수_추출_결과.txt")
    excel_save_path = os.path.join(BASE_DIR, "도면_치수_추출_결과.xlsx")
    
    try:
        with open(txt_save_path, "w", encoding="utf-8") as f:
            f.write(final_output)
            
        save_to_excel(final_output, excel_save_path)
        
        print("\n[완료] 모든 파일 저장이 성공적으로 완료되었습니다!")
        print(f"1. 텍스트 보고서 저장 완료: {txt_save_path}")
        print(f"2. 정렬된 엑셀 표 저장 완료: {excel_save_path}")
        
    except Exception as e:
        print(f"\n[오류] 파일 자동 저장 중 오류 발생: {e}")

if __name__ == "__main__":
    main()
