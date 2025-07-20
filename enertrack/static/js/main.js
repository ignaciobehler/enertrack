const btnDelete= document.querySelectorAll('.btn-borrar');
if(btnDelete) {
  const btnArray = Array.from(btnDelete);
  btnArray.forEach((btn) => {
    btn.addEventListener('click', (e) => {
      if(!confirm('¿Está seguro de querer borrar?')){
        e.preventDefault();
      }
    });
  })
}

document.addEventListener('DOMContentLoaded', () => {
  const themeLink = document.getElementById('theme-link');
  const toggleBtn = document.getElementById('theme-toggle');

  // URLs de los temas
  const themes = {
    light: 'https://bootswatch.com/5/flatly/bootstrap.min.css',
    dark:  'https://bootswatch.com/5/darkly/bootstrap.min.css'
  };

  // Carga la preferencia almacenada (si existe)
  const saved = localStorage.getItem('theme') || 'light';
  themeLink.setAttribute('href', themes[saved]);

  // Ajusta el texto del botón
  toggleBtn.textContent = saved === 'dark' ? '☀︎' : '☾';

  // Al hacer clic, alterna tema
  toggleBtn.addEventListener('click', () => {
    const current = localStorage.getItem('theme') || 'light';
    const next = current === 'dark' ? 'light' : 'dark';

    themeLink.setAttribute('href', themes[next]);
    localStorage.setItem('theme', next);
    toggleBtn.textContent = next === 'dark' ? '☀︎' : '☾';
  });
});

