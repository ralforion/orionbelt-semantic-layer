// Fix for Google Analytics feedback tracking
// MkDocs Material doesn't properly define gtag, so we define it here

// Initialize gtag function if not already defined
if (typeof window.gtag === 'undefined') {
  window.dataLayer = window.dataLayer || [];
  window.gtag = function(){
    dataLayer.push(arguments);
  }
  
  // Initialize with current time
  gtag('js', new Date());
  
  // Configure GA4 property
  gtag('config', 'G-X19K4GX3EX', {
    'anonymize_ip': true,
    'cookie_flags': 'SameSite=None;Secure'
  });
  
  console.log('Google Analytics initialized with gtag function');
}

// Enhanced feedback tracking
document.addEventListener('DOMContentLoaded', function() {
  // Wait for MkDocs to set up the feedback form
  setTimeout(function() {
    const feedbackForm = document.querySelector('.md-feedback');
    if (feedbackForm) {
      const buttons = feedbackForm.querySelectorAll('button[data-md-value]');
      
      buttons.forEach(button => {
        button.addEventListener('click', function(e) {
          const value = this.getAttribute('data-md-value');
          const page = window.location.pathname;
          
          // Send event to GA4
          if (typeof gtag !== 'undefined') {
            gtag('event', 'page_feedback', {
              'event_category': 'engagement',
              'event_label': page,
              'value': parseInt(value),
              'page_path': page,
              'feedback_value': value === '1' ? 'helpful' : 'not_helpful'
            });
            
            console.log('Feedback sent to GA4:', {
              page: page,
              value: value === '1' ? 'helpful' : 'not_helpful'
            });
          }
        });
      });
      
      console.log('Feedback tracking enhanced');
    }
  }, 1000);
});

// Also track page views properly
document.addEventListener('DOMContentLoaded', function() {
  if (typeof gtag !== 'undefined') {
    gtag('event', 'page_view', {
      page_path: window.location.pathname,
      page_title: document.title
    });
  }
});