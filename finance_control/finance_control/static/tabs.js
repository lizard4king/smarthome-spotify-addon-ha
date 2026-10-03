'use strict';

const cockpitNav=document.querySelector('nav[aria-label="Bereiche"]');
const cockpitAnnouncement=document.getElementById('active-area-announcement');

function cockpitTabs(){
  return [...cockpitNav.querySelectorAll('a[href^="#"]')];
}

function cockpitNarrowView(){
  return typeof window.matchMedia==='function'&&window.matchMedia('(max-width:760px)').matches;
}

function cockpitRevealTab(tab){
  if(cockpitNarrowView()&&typeof tab.scrollIntoView==='function'){
    tab.scrollIntoView({block:'nearest',inline:'center'});
  }
}

function cockpitShowTab(id,{focus=false}={}){
  const tabs=cockpitTabs();
  if(!tabs.length)return;
  const selected=tabs.find(tab=>tab.hash==='#'+id)||tabs[0];
  for(const tab of tabs){
    const active=tab===selected;
    tab.setAttribute('role','tab');
    tab.id='tab-'+tab.hash.slice(1);
    tab.setAttribute('aria-controls',tab.hash.slice(1));
    tab.setAttribute('aria-selected',String(active));
    if(active)tab.setAttribute('aria-current','page');
    else tab.removeAttribute('aria-current');
    tab.tabIndex=active?0:-1;
    const panel=document.getElementById(tab.hash.slice(1));
    if(panel){
      panel.hidden=!active;
      panel.setAttribute('role','tabpanel');
      panel.setAttribute('aria-labelledby',tab.id);
    }
  }
  if(cockpitAnnouncement){
    const text=`Aktueller Bereich: ${selected.textContent.trim()}.`;
    if(cockpitAnnouncement.textContent!==text)cockpitAnnouncement.textContent=text;
  }
  cockpitRevealTab(selected);
  if(focus)selected.focus();
}

function cockpitNavigate(id,{focus=false}={}){
  if(location.hash!=='#'+id)location.hash='#'+id;
  cockpitShowTab(id,{focus});
}

cockpitNav.setAttribute('role','tablist');
cockpitNav.setAttribute('aria-orientation','horizontal');
cockpitNav.addEventListener('keydown',event=>{
  const tabs=[...cockpitNav.querySelectorAll('[role="tab"]')];
  const index=tabs.indexOf(document.activeElement);
  if(index<0)return;
  let next=index;
  if(event.key==='ArrowRight')next=(index+1)%tabs.length;
  else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;
  else if(event.key==='Home')next=0;
  else if(event.key==='End')next=tabs.length-1;
  else return;
  event.preventDefault();
  cockpitNavigate(tabs[next].hash.slice(1),{focus:true});
});
window.addEventListener('hashchange',()=>cockpitShowTab(location.hash.slice(1)));
cockpitShowTab(location.hash.slice(1));
