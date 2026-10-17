const menu = document.getElementById('siteMenu');
const openMenu = document.getElementById('menuOpen');
openMenu.addEventListener('click', () => { menu.showModal(); openMenu.setAttribute('aria-expanded','true'); document.body.classList.add('menu-is-open'); });
document.getElementById('menuClose').addEventListener('click', () => menu.close());
menu.addEventListener('close', () => {openMenu.setAttribute('aria-expanded','false'); document.body.classList.remove('menu-is-open'); openMenu.focus();});
menu.querySelectorAll('a').forEach(link => link.addEventListener('click', event => {
  event.preventDefault();
  const destination = link.href;
  menu.close();
  window.location.assign(destination);
}));

const cards = document.querySelector('.athlete-cards');
function filterHome() {
  const season = document.getElementById('season').value;
  const search = document.getElementById('athleteSearch').value.trim().toLowerCase();
  let count = 0;
  document.querySelectorAll('.meet-year').forEach(el => el.hidden = season !== 'all' && el.dataset.year !== season);
  cards.querySelectorAll('.athlete-card').forEach(el => {
    el.hidden = !el.dataset.name.includes(search) || (season !== 'all' && !el.dataset.seasons.split(' ').includes(season));
    if (!el.hidden) count++;
  });
  document.getElementById('athleteCount').textContent = count;
  document.getElementById('noAthletes').hidden = count !== 0;
}
if (cards) {
  document.getElementById('season').addEventListener('change',filterHome);
  document.getElementById('athleteSearch').addEventListener('input',filterHome);
  document.getElementById('athleteSort').addEventListener('change',event => {
    const key = event.target.value;
    [...cards.children].sort((a,b) => key === 'name' ? a.dataset.name.localeCompare(b.dataset.name) : Number(a.dataset[key])-Number(b.dataset[key]) || a.dataset.name.localeCompare(b.dataset.name)).forEach(el => cards.appendChild(el));
  });
}
document.getElementById('homeOnly')?.addEventListener('change',event => document.querySelectorAll('tr.other').forEach(row=>row.hidden=event.target.checked));
document.querySelectorAll('.tab').forEach(button => button.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(tab=>{tab.classList.toggle('active',tab===button);tab.setAttribute('aria-pressed',String(tab===button));});
  document.querySelectorAll('.race-section').forEach(section=>section.hidden=button.dataset.race!=='all' && section.dataset.raceId!==button.dataset.race);
}));
