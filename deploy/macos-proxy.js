// The proxy macOS (and so the browser) uses for a URL, for command-line tools that only read HTTPS_PROXY.
// Follows the system settings: a set HTTPS proxy, a PAC file, or auto-discovery (WPAD).
// Prints http://host:port, or nothing when the URL goes direct. Run by make: osascript -l JavaScript deploy/macos-proxy.js URL
ObjC.import('Foundation');
ObjC.import('CFNetwork');

const PROXY = ['kCFProxyTypeHTTP', 'kCFProxyTypeHTTPS'];

// The first proxy in a CFNetwork proxy list: http://host:port, '' for direct, null for a PAC entry (with its URL)
function pick(ref, onPac) {
  const list = ObjC.castRefToObject(ref);
  for (let i = 0; !list.isNil() && i < list.count; i++) {
    const entry = list.objectAtIndex(i);
    const type = ObjC.unwrap(entry.objectForKey('kCFProxyTypeKey'));
    if (PROXY.includes(type))
      return `http://${ObjC.unwrap(entry.objectForKey('kCFProxyHostNameKey'))}:${ObjC.unwrap(entry.objectForKey('kCFProxyPortNumberKey'))}`;
    if (type === 'kCFProxyTypeNone') return '';
    if (type === 'kCFProxyTypeAutoConfigurationURL' && onPac) {
      const found = onPac(entry.objectForKey('kCFProxyAutoConfigurationURLKey'));
      if (found !== null) return found;
    }
  }
  return null;
}

function proxyFor(url, settings) {
  return pick($.CFNetworkCopyProxiesForURL(url, settings), pacUrl => {
    const script = $.NSString.stringWithContentsOfURLEncodingError(pacUrl, $.NSUTF8StringEncoding, null);
    return script.isNil() ? null : pick($.CFNetworkCopyProxiesForAutoConfigurationScript(script, url, null));
  }) || '';
}

// A second argument, JSON proxy settings in place of the system's, is for tests
function run(argv) {
  const settings = argv[1] ? $(JSON.parse(argv[1])) : $.CFNetworkCopySystemProxySettings();
  return proxyFor($.NSURL.URLWithString(argv[0] || 'https://github.com'), settings);
}
