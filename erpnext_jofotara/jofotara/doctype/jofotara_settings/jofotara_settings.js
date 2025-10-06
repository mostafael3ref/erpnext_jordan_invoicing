frappe.ui.form.on('JoFotara Settings', {
  refresh(frm) {
    frm.add_custom_button(__('Open in Desk'), () => {
      window.location.href = '/app/jo-fotara-settings';
    });
  }
});
