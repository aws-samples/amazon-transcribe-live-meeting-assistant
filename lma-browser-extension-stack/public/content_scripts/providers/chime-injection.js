const checkVariable = setInterval(() => {
  const titles = document.querySelectorAll('[data-test-id="meetingTitle"]');
  if (titles.length > 0) {
    console.log('found title');
    window.postMessage({ type: "Title", value: titles[0].innerHTML });
    clearInterval(checkVariable);
  } else {
    console.log('title not yet defined');
  }
}, 1000);