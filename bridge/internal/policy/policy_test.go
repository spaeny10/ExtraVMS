package policy

import (
	"net/netip"
	"testing"

	"axiomvision.ai/bridge/internal/proto"
)

func ownLAN() ([]netip.Prefix, error) {
	return []netip.Prefix{netip.MustParsePrefix("192.168.1.20/24"), netip.MustParsePrefix("127.0.0.1/8")}, nil
}

func TestClassify(t *testing.T) {
	denied := []string{"127.0.0.1", "127.9.9.9", "::1", "169.254.169.254", "169.254.1.1", "fe80::1", "224.0.0.1",
		"239.255.255.250", "ff02::1", "0.0.0.0", "0.1.2.3", "::", "255.255.255.255", "240.0.0.1",
		"8.8.8.8", "100.64.0.1", "2001:db8::1", "fd00::1", "::ffff:127.0.0.1"}
	for _, s := range denied {
		if err := Classify(netip.MustParseAddr(s), false); err == nil || !IsDenied(err) {
			t.Errorf("%s allowed", s)
		}
	}
	for _, s := range []string{"10.0.0.5", "172.16.3.4", "172.31.255.1", "192.168.1.50", "::ffff:192.168.1.50"} {
		if err := Classify(netip.MustParseAddr(s), false); err != nil {
			t.Errorf("%s: %v", s, err)
		}
	}
	// allow_public opens public addresses but none of the hard denies
	for _, s := range []string{"8.8.8.8", "100.64.0.1", "2001:db8::1"} {
		if err := Classify(netip.MustParseAddr(s), true); err != nil {
			t.Errorf("%s with allow_public: %v", s, err)
		}
	}
	for _, s := range []string{"127.0.0.1", "169.254.169.254", "224.0.0.1", "255.255.255.255", "0.0.0.0"} {
		if Classify(netip.MustParseAddr(s), true) == nil {
			t.Errorf("%s allowed with allow_public", s)
		}
	}
}

func TestCheckNeedsAssignment(t *testing.T) {
	p := New(Options{OwnAddrs: ownLAN})
	if _, err := p.Check("bt_1", "192.168.1.50", 554); err == nil {
		t.Fatal("allowed before any targets")
	}
	p.SetTargets(1, []proto.Target{{ID: "bt_1", Host: "192.168.1.50", Ports: []int{554, 80}}})
	if ap, err := p.Check("bt_1", "192.168.1.50", 554); err != nil || ap.String() != "192.168.1.50:554" {
		t.Fatalf("assigned: %v %v", ap, err)
	}
	for _, c := range []struct {
		target, host string
		port         int
	}{
		{"bt_1", "192.168.1.50", 555},   // port not assigned
		{"bt_2", "192.168.1.50", 554},   // other target id
		{"bt_1", "192.168.1.51", 554},   // other host
		{"bt_1", "camera.local", 554},   // DNS name
		{"bt_1", "192.168.1.50", 0},     // bad port
		{"bt_1", "192.168.1.50", 70000}, // bad port
	} {
		if _, err := p.Check(c.target, c.host, c.port); err == nil {
			t.Errorf("%+v allowed", c)
		}
	}
	// replacing the list drops the old assignment
	p.SetTargets(2, []proto.Target{{ID: "bt_2", Host: "192.168.1.60", Ports: []int{554}}})
	if p.Allowed("bt_1", netip.MustParseAddrPort("192.168.1.50:554")) {
		t.Fatal("old target still allowed")
	}
	if rev, n := p.Summary(); rev != 2 || n != 1 {
		t.Fatalf("summary %d %d", rev, n)
	}
}

func TestHardDeniesBeatAssignment(t *testing.T) {
	p := New(Options{OwnAddrs: ownLAN})
	bad := p.SetTargets(1, []proto.Target{
		{ID: "lo", Host: "127.0.0.1", Ports: []int{554}},
		{ID: "ll", Host: "169.254.169.254", Ports: []int{80}},
		{ID: "pub", Host: "8.8.8.8", Ports: []int{53}},
		{ID: "own", Host: "192.168.1.20", Ports: []int{3389}},
		{ID: "bcast", Host: "192.168.1.255", Ports: []int{80}},
		{ID: "net", Host: "192.168.1.0", Ports: []int{80}},
		{ID: "name", Host: "nas.lan", Ports: []int{80}},
	})
	if len(bad) != 1 || bad[0] != "name" {
		t.Fatalf("bad targets %v", bad)
	}
	for _, c := range []struct {
		id, host string
		port     int
	}{{"lo", "127.0.0.1", 554}, {"ll", "169.254.169.254", 80}, {"pub", "8.8.8.8", 53}, {"own", "192.168.1.20", 3389},
		{"bcast", "192.168.1.255", 80}, {"net", "192.168.1.0", 80}, {"name", "nas.lan", 80}} {
		if _, err := p.Check(c.id, c.host, c.port); err == nil || !IsDenied(err) {
			t.Errorf("%s allowed", c.id)
		}
	}
	// allow_public lets the public one through, nothing else
	pp := New(Options{OwnAddrs: ownLAN, AllowPublic: true})
	pp.SetTargets(1, []proto.Target{{ID: "pub", Host: "8.8.8.8", Ports: []int{53}}, {ID: "lo", Host: "127.0.0.1", Ports: []int{554}}})
	if _, err := pp.Check("pub", "8.8.8.8", 53); err != nil {
		t.Fatal(err)
	}
	if _, err := pp.Check("lo", "127.0.0.1", 554); err == nil {
		t.Fatal("loopback allowed with allow_public")
	}
}

func TestAllowList(t *testing.T) {
	allow, err := ParseAllow([]string{"192.168.1.50", "10.1.0.0/16,  "})
	if err != nil {
		t.Fatal(err)
	}
	p := New(Options{OwnAddrs: ownLAN, Allow: allow})
	p.SetTargets(1, []proto.Target{
		{ID: "a", Host: "192.168.1.50", Ports: []int{554}},
		{ID: "b", Host: "192.168.1.51", Ports: []int{554}},
		{ID: "c", Host: "10.1.4.4", Ports: []int{80}},
	})
	if _, err := p.Check("a", "192.168.1.50", 554); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Check("c", "10.1.4.4", 80); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Check("b", "192.168.1.51", 554); err == nil {
		t.Fatal("address outside the allow list accepted")
	}
	if _, err := ParseAllow([]string{"camera"}); err == nil {
		t.Fatal("ParseAllow accepted a name")
	}
}

func TestExemptHookStillNeedsAssignmentAndAllowList(t *testing.T) {
	loop := func(ap netip.AddrPort) bool { return ap.Addr().IsLoopback() }
	p := New(Options{OwnAddrs: ownLAN, Exempt: loop})
	p.SetTargets(1, []proto.Target{{ID: "echo", Host: "127.0.0.1", Ports: []int{9000}}})
	if _, err := p.Check("echo", "127.0.0.1", 9000); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Check("echo", "127.0.0.1", 9001); err == nil {
		t.Fatal("unassigned port allowed")
	}
	allow, _ := ParseAllow([]string{"192.168.1.50"})
	q := New(Options{OwnAddrs: ownLAN, Exempt: loop, Allow: allow})
	q.SetTargets(1, []proto.Target{{ID: "echo", Host: "127.0.0.1", Ports: []int{9000}}})
	if _, err := q.Check("echo", "127.0.0.1", 9000); err == nil {
		t.Fatal("allow list skipped")
	}
}

func TestLastAddr(t *testing.T) {
	if got := lastAddr(netip.MustParsePrefix("192.168.1.20/24")); got.String() != "192.168.1.255" {
		t.Fatal(got)
	}
	if got := lastAddr(netip.MustParsePrefix("10.0.0.1/8")); got.String() != "10.255.255.255" {
		t.Fatal(got)
	}
}

func TestInterfacePrefixesAndLAN(t *testing.T) {
	pre, err := InterfacePrefixes()
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range pre {
		if !p.IsValid() {
			t.Errorf("invalid prefix %v", p)
		}
	}
	for _, l := range LAN() {
		if _, err := netip.ParsePrefix(l.Addr); err != nil || l.If == "" {
			t.Errorf("bad LAN entry %+v", l)
		}
	}
}
