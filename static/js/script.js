// Minimal helpers for JuiceFront
document.addEventListener('DOMContentLoaded', () => {
  // Auto-dismiss flash after 5s
  document.querySelectorAll('.flash').forEach(el => {
    setTimeout(() => { el.style.transition='opacity .4s'; el.style.opacity=0;
      setTimeout(()=>el.remove(),400); }, 5000);
  });
  // Client-side file size guard (2MB)
  document.querySelectorAll('input[type=file]').forEach(inp => {
    inp.addEventListener('change', () => {
      const f = inp.files[0];
      if (f && f.size > 2*1024*1024) {
        alert('Image must be under 2 MB.');
        inp.value = '';
      }
    });
  });
});
