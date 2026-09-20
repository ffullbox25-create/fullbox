from __future__ import annotations

import base64

import fitz


LABEL_WIDTH_MM = 75
LABEL_HEIGHT_MM = 120
LABEL_DOTS_PER_MM = 12
PRINT_DPI = 203
PREVIEW_CONTENT_SCALE = 0.92
LABEL_WIDTH_PX = LABEL_WIDTH_MM * LABEL_DOTS_PER_MM
LABEL_HEIGHT_PX = LABEL_HEIGHT_MM * LABEL_DOTS_PER_MM
PRINT_WIDTH_PX = round(LABEL_WIDTH_MM * PRINT_DPI / 25.4)
PRINT_HEIGHT_PX = round(LABEL_HEIGHT_MM * PRINT_DPI / 25.4)
_POINTS_PER_MM = 72 / 25.4
_LABEL_WIDTH_PT = LABEL_WIDTH_MM * _POINTS_PER_MM
_LABEL_HEIGHT_PT = LABEL_HEIGHT_MM * _POINTS_PER_MM


def _content_rect(scale: float) -> fitz.Rect:
    inset_x = _LABEL_WIDTH_PT * (1 - scale) / 2
    inset_y = _LABEL_HEIGHT_PT * (1 - scale) / 2
    return fitz.Rect(
        inset_x,
        inset_y,
        _LABEL_WIDTH_PT - inset_x,
        _LABEL_HEIGHT_PT - inset_y,
    )


def _render_page_png(page: fitz.Page, *, width_px: int, height_px: int) -> bytes:
    matrix = fitz.Matrix(
        width_px / _LABEL_WIDTH_PT,
        height_px / _LABEL_HEIGHT_PT,
    )
    pixmap = page.get_pixmap(
        matrix=matrix,
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    if pixmap.width != width_px or pixmap.height != height_px:
        raise ValueError("Не удалось привести этикетку Ozon к размеру 75x120 мм.")
    return pixmap.tobytes("png")


def _positive_int(value) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _barcodes_by_supply(gm_cargoes: list[dict]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for row in gm_cargoes or []:
        if not isinstance(row, dict):
            continue
        supply_id = _positive_int(row.get("supply_id"))
        barcode = str(row.get("gm_barcode") or "").strip()
        if not supply_id or not barcode:
            continue
        bucket = result.setdefault(supply_id, [])
        if barcode not in bucket:
            bucket.append(barcode)
    return result


def _compact_page_text(value: str) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def _page_indexes_by_barcode(source: fitz.Document, supply_barcodes: list[str], supply_id: int) -> dict[str, int]:
    barcode_by_token: dict[str, str] = {}
    for barcode in supply_barcodes:
        token = _compact_page_text(barcode)
        if not token or token in barcode_by_token:
            raise ValueError(f"Не удалось однозначно определить ШК ГМ Ozon для поставки {supply_id}.")
        barcode_by_token[token] = barcode

    page_indexes: dict[str, int] = {}
    for page_index in range(source.page_count):
        page_text = _compact_page_text(source.load_page(page_index).get_text("text"))
        matches = [barcode for token, barcode in barcode_by_token.items() if token in page_text]
        if len(matches) != 1 or matches[0] in page_indexes:
            raise ValueError(
                "Не удалось сопоставить страницы PDF Ozon с ШК ГМ "
                f"для поставки {supply_id}. Печать остановлена."
            )
        page_indexes[matches[0]] = page_index

    if len(page_indexes) != len(supply_barcodes):
        raise ValueError(
            "Не удалось сопоставить все страницы PDF Ozon с ШК ГМ "
            f"для поставки {supply_id}. Печать остановлена."
        )
    return page_indexes


def build_ozon_gm_label_assets(documents: list[dict], gm_cargoes: list[dict]) -> dict:
    """Fit official Ozon PDF pages onto 75x120 mm and prepare print previews."""
    barcodes = _barcodes_by_supply(gm_cargoes)
    if not documents:
        raise ValueError("Ozon не вернул PDF с этикетками ШК ГМ.")
    if not barcodes:
        raise ValueError("В заявке нет сохранённых ШК ГМ Ozon для печати.")

    combined = fitz.open()
    labels: list[dict] = []
    try:
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("Ozon вернул повреждённый документ этикеток.")
            supply_id = _positive_int(document.get("supply_id"))
            content = document.get("content")
            if not supply_id or not isinstance(content, (bytes, bytearray)) or not content:
                raise ValueError("Ozon вернул повреждённый PDF с этикетками.")
            supply_barcodes = barcodes.get(supply_id) or []
            try:
                source = fitz.open(stream=bytes(content), filetype="pdf")
            except Exception as exc:
                raise ValueError("Не удалось прочитать PDF этикеток Ozon.") from exc
            try:
                if source.page_count != len(supply_barcodes):
                    raise ValueError(
                        "Количество страниц в PDF Ozon не совпадает с количеством ШК ГМ "
                        f"для поставки {supply_id}: {source.page_count} вместо {len(supply_barcodes)}."
                    )
                page_indexes = _page_indexes_by_barcode(source, supply_barcodes, supply_id)
                for label_index, barcode in enumerate(supply_barcodes):
                    page_index = page_indexes[barcode]
                    source_page = source.load_page(page_index)
                    rotate = 90 if source_page.rect.width > source_page.rect.height else 0
                    single = fitz.open()
                    try:
                        target_page = single.new_page(width=_LABEL_WIDTH_PT, height=_LABEL_HEIGHT_PT)
                        target_page.show_pdf_page(
                            _content_rect(PREVIEW_CONTENT_SCALE),
                            source,
                            page_index,
                            keep_proportion=True,
                            rotate=rotate,
                        )
                        png = _render_page_png(
                            target_page,
                            width_px=LABEL_WIDTH_PX,
                            height_px=LABEL_HEIGHT_PX,
                        )
                        print_png = _render_page_png(
                            target_page,
                            width_px=PRINT_WIDTH_PX,
                            height_px=PRINT_HEIGHT_PX,
                        )
                        pdf = single.tobytes(garbage=4, deflate=True)
                        combined.insert_pdf(single)
                    finally:
                        single.close()
                    labels.append(
                        {
                            "kind": "ozon_gm",
                            "slip_key": f"OZON-GM-{supply_id}-{label_index + 1}",
                            "gm_barcode": barcode,
                            "shipping_barcode": barcode,
                            "pallet_label": barcode,
                            "supply_id": str(supply_id),
                            "label_width_mm": LABEL_WIDTH_MM,
                            "label_height_mm": LABEL_HEIGHT_MM,
                            "label_png_base64": base64.b64encode(png).decode("ascii"),
                            "print_png_base64": base64.b64encode(print_png).decode("ascii"),
                            "pdf_base64": base64.b64encode(pdf).decode("ascii"),
                        }
                    )
            finally:
                source.close()

        if not labels:
            raise ValueError("В PDF Ozon не найдено ни одной этикетки ШК ГМ.")
        total = len(labels)
        for index, label in enumerate(labels, start=1):
            label["label_index"] = str(index)
            label["label_counter"] = f"{index} из {total}"
        combined_pdf = combined.tobytes(garbage=4, deflate=True)
        return {
            "labels": labels,
            "combined_pdf_base64": base64.b64encode(combined_pdf).decode("ascii"),
            "label_count": total,
            "label_width_mm": LABEL_WIDTH_MM,
            "label_height_mm": LABEL_HEIGHT_MM,
        }
    finally:
        combined.close()
