"""Advance existing import scenarios through the mandatory mapping review."""
def advance_wizard(wizard):
    if wizard.page_stack.currentIndex() == 1 and not wizard._mapping_review_complete():
        wizard._open_mapping_dialog()
        wizard.mapping_dialog.done_button.click()
    wizard._go_next()


def select_single_header_source(wizard, path):
    """Explicitly select one-row headers for synthetic one-row Excel fixtures."""
    wizard.path_edit.setText(str(path))
    blocked = wizard.header_count_combo.blockSignals(True)
    try:
        wizard.header_count_combo.setCurrentIndex(wizard.header_count_combo.findData(1))
    finally:
        wizard.header_count_combo.blockSignals(blocked)
