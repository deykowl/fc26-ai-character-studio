const form = document.querySelector('#login-form');
const error = document.querySelector('#login-error');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.textContent = '';
  const body = new FormData();
  body.append('code', document.querySelector('#code').value);
  const response = await fetch('/api/login', {method: 'POST', body});
  if (response.ok) { location.href = '/'; return; }
  const result = await response.json().catch(() => ({}));
  error.textContent = result.error || 'Connexion refusée.';
});
