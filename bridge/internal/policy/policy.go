// Package policy decides which camera addresses the bridge may connect to. The hub assigns targets, but the bridge
// never trusts that alone: an address must be assigned AND pass the local rules (no loopback, link-local or cloud
// metadata, multicast, broadcast, the PC's own addresses, public addresses unless allow_public, or DNS names), AND be
// in the owner's allow list when one is configured. A compromised hub therefore cannot turn the bridge into a proxy
// to the PC itself or to the internet.
package policy

import (
	"errors"
	"fmt"
	"net"
	"net/netip"
	"sort"
	"strings"
	"sync"
	"time"

	"axiomvision.ai/bridge/internal/proto"
)

// Options are the owner's local settings (config.json) plus hooks for tests.
type Options struct {
	AllowPublic bool           // permit non-RFC1918 addresses (a camera on a public or CGNAT address)
	Allow       []netip.Prefix // when non-empty, only these addresses may be reached, whatever the hub assigns
	// OwnAddrs lists this PC's interface addresses; nil reads them from the OS (cached for ownTTL).
	OwnAddrs func() ([]netip.Prefix, error)
	// Exempt lets an address skip the local address rules (it must still be assigned and allow-listed).
	// For tests only (a 127.0.0.1 echo server); nothing in the bridge's configuration sets it.
	Exempt func(netip.AddrPort) bool
}

// Denied is why an address was refused; its text goes back to the hub in abort / probe_result.
type Denied struct{ Reason string }

func (d *Denied) Error() string { return "denied: " + d.Reason }

func deny(format string, a ...any) error { return &Denied{Reason: fmt.Sprintf(format, a...)} }

// IsDenied reports whether err is a policy refusal (as opposed to a connect failure).
func IsDenied(err error) bool {
	var d *Denied
	return errors.As(err, &d)
}

type key struct {
	target string
	ap     netip.AddrPort
}

// Policy holds the hub's current target list, replaced atomically by each `targets` frame.
type Policy struct {
	opts Options

	mu      sync.RWMutex
	rev     int64
	allowed map[key]struct{}
	n       int // targets in the list

	ownMu sync.Mutex
	ownAt time.Time
	own   []netip.Prefix
}

const ownTTL = 30 * time.Second

// New returns a policy with no targets: nothing may be reached until the hub sends its list.
func New(o Options) *Policy {
	return &Policy{opts: o, allowed: map[key]struct{}{}}
}

// ParseAllow reads the owner's allow list: IP addresses ("192.168.1.50") or prefixes ("192.168.1.0/24").
func ParseAllow(list []string) ([]netip.Prefix, error) {
	var out []netip.Prefix
	for _, s := range list {
		for _, part := range strings.Split(s, ",") {
			part = strings.TrimSpace(part)
			if part == "" {
				continue
			}
			if strings.Contains(part, "/") {
				p, err := netip.ParsePrefix(part)
				if err != nil {
					return nil, fmt.Errorf("allow %q: %w", part, err)
				}
				out = append(out, p.Masked())
				continue
			}
			a, err := netip.ParseAddr(part)
			if err != nil {
				return nil, fmt.Errorf("allow %q: not an IP address or prefix", part)
			}
			a = a.Unmap()
			out = append(out, netip.PrefixFrom(a, a.BitLen()))
		}
	}
	return out, nil
}

// SetTargets replaces the target list. Items whose host is not an IP literal are kept out (they could never pass)
// and returned so the caller can log them.
func (p *Policy) SetTargets(rev int64, items []proto.Target) (bad []string) {
	allowed := map[key]struct{}{}
	for _, it := range items {
		a, err := parseHost(it.Host)
		if err != nil {
			bad = append(bad, it.ID)
			continue
		}
		for _, port := range it.Ports {
			if port > 0 && port < 65536 {
				allowed[key{it.ID, netip.AddrPortFrom(a, uint16(port))}] = struct{}{}
			}
		}
	}
	p.mu.Lock()
	p.rev, p.allowed, p.n = rev, allowed, len(items)
	p.mu.Unlock()
	return bad
}

// Summary is the current list's revision and size, for logs and the status file.
func (p *Policy) Summary() (rev int64, targets int) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.rev, p.n
}

// Check is the gate for `open` and `probe`: the address to dial, or why not.
func (p *Policy) Check(target, host string, port int) (netip.AddrPort, error) {
	a, err := parseHost(host)
	if err != nil {
		return netip.AddrPort{}, err
	}
	if port <= 0 || port > 65535 {
		return netip.AddrPort{}, deny("port %d out of range", port)
	}
	ap := netip.AddrPortFrom(a, uint16(port))
	if err := p.local(ap); err != nil {
		return netip.AddrPort{}, err
	}
	if !p.assigned(target, ap) {
		return netip.AddrPort{}, deny("%s is not assigned to target %q", ap, target)
	}
	return ap, nil
}

// Allowed re-checks an open stream after the target list changed.
func (p *Policy) Allowed(target string, ap netip.AddrPort) bool {
	return p.assigned(target, ap) && p.local(ap) == nil
}

func (p *Policy) assigned(target string, ap netip.AddrPort) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	_, ok := p.allowed[key{target, ap}]
	return ok
}

// local applies the rules that do not depend on the hub: address class, own addresses, allow list.
func (p *Policy) local(ap netip.AddrPort) error {
	a := ap.Addr()
	if p.opts.Exempt == nil || !p.opts.Exempt(ap) {
		if err := Classify(a, p.opts.AllowPublic); err != nil {
			return err
		}
		if p.isOwn(a) {
			return deny("%s is one of this PC's own addresses", a)
		}
	}
	if len(p.opts.Allow) > 0 {
		for _, pre := range p.opts.Allow {
			if pre.Contains(a) {
				return nil
			}
		}
		return deny("%s is not in this bridge's allow list", a)
	}
	return nil
}

// parseHost accepts only IP literals: a DNS name would be resolved on the site's network, out of the hub's sight.
func parseHost(host string) (netip.Addr, error) {
	a, err := netip.ParseAddr(strings.TrimSpace(host))
	if err != nil {
		return netip.Addr{}, deny("%q is not an IP address (names are not allowed)", host)
	}
	if a.Zone() != "" {
		return netip.Addr{}, deny("%q has an IPv6 zone", host)
	}
	return a.Unmap(), nil
}

var (
	rfc1918   = []netip.Prefix{netip.MustParsePrefix("10.0.0.0/8"), netip.MustParsePrefix("172.16.0.0/12"), netip.MustParsePrefix("192.168.0.0/16")}
	thisNet   = netip.MustParsePrefix("0.0.0.0/8")
	reserved  = netip.MustParsePrefix("240.0.0.0/4") // includes 255.255.255.255
	broadcast = netip.MustParseAddr("255.255.255.255")
)

// Classify applies the address-class rules: loopback, link-local (169.254/16 holds cloud metadata), multicast,
// unspecified, broadcast and reserved are always refused; anything outside RFC 1918 needs allowPublic.
func Classify(a netip.Addr, allowPublic bool) error {
	a = a.Unmap()
	switch {
	case !a.IsValid():
		return deny("invalid address")
	case a.IsUnspecified() || (a.Is4() && thisNet.Contains(a)):
		return deny("%s is unspecified", a)
	case a.IsLoopback():
		return deny("%s is a loopback address", a)
	case a.IsLinkLocalUnicast():
		return deny("%s is link-local", a)
	case a.IsMulticast() || a.IsLinkLocalMulticast() || a.IsInterfaceLocalMulticast():
		return deny("%s is multicast", a)
	case a == broadcast:
		return deny("%s is broadcast", a)
	case a.Is4() && reserved.Contains(a):
		return deny("%s is reserved", a)
	}
	for _, p := range rfc1918 {
		if p.Contains(a) {
			return nil
		}
	}
	if allowPublic {
		return nil
	}
	return deny("%s is not a private (RFC 1918) address; set allow_public to permit it", a)
}

// isOwn: the PC's own addresses, and the network and broadcast addresses of its IPv4 subnets.
func (p *Policy) isOwn(a netip.Addr) bool {
	for _, pre := range p.ownAddrs() {
		if pre.Addr() == a {
			return true
		}
		if a.Is4() && pre.Addr().Is4() && pre.Bits() < 31 && pre.Contains(a) {
			if a == pre.Masked().Addr() || a == lastAddr(pre) {
				return true
			}
		}
	}
	return false
}

func (p *Policy) ownAddrs() []netip.Prefix {
	p.ownMu.Lock()
	defer p.ownMu.Unlock()
	if p.own != nil && time.Since(p.ownAt) < ownTTL {
		return p.own
	}
	read := p.opts.OwnAddrs
	if read == nil {
		read = InterfacePrefixes
	}
	own, err := read()
	if err != nil && p.own != nil {
		return p.own // keep the last good list rather than forget the PC's addresses
	}
	if own == nil {
		own = []netip.Prefix{}
	}
	p.own, p.ownAt = own, time.Now()
	return p.own
}

// lastAddr is an IPv4 prefix's broadcast address.
func lastAddr(p netip.Prefix) netip.Addr {
	b := p.Masked().Addr().As4()
	host := uint32(1)<<(32-p.Bits()) - 1
	v := uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3]) | host
	return netip.AddrFrom4([4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)})
}

// InterfacePrefixes is every address on every interface of this PC, with its prefix length.
func InterfacePrefixes() ([]netip.Prefix, error) {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return nil, err
	}
	out := make([]netip.Prefix, 0, len(addrs))
	for _, ad := range addrs {
		if n, ok := ad.(*net.IPNet); ok {
			if a, ok := netip.AddrFromSlice(n.IP); ok {
				ones, _ := n.Mask.Size()
				a = a.Unmap()
				if a.Is4() && ones > 32 {
					ones -= 96
				}
				out = append(out, netip.PrefixFrom(a, ones))
			}
		}
	}
	return out, nil
}

// LAN lists the PC's IPv4 addresses on interfaces that are up (for hello.lan), sorted for a stable display.
func LAN() []proto.LANAddr {
	ifs, err := net.Interfaces()
	if err != nil {
		return []proto.LANAddr{}
	}
	out := []proto.LANAddr{}
	for _, ifc := range ifs {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, ad := range addrs {
			n, ok := ad.(*net.IPNet)
			if !ok {
				continue
			}
			ip4 := n.IP.To4()
			if ip4 == nil || ip4.IsLinkLocalUnicast() {
				continue
			}
			ones, bits := n.Mask.Size()
			if bits == 128 {
				ones -= 96
			}
			out = append(out, proto.LANAddr{If: ifc.Name, Addr: fmt.Sprintf("%s/%d", ip4, ones)})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].If+out[i].Addr < out[j].If+out[j].Addr })
	return out
}
