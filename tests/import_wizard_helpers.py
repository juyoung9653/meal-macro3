"""Advance existing import scenarios through the mandatory mapping review."""
def advance_wizard(wizard):
    if wizard.page_stack.currentIndex() == 1 and not wizard._mapping_review_complete():
        wizard._open_mapping_dialog()
        wizard.mapping_dialog.done_button.click()
    wizard._go_next()
