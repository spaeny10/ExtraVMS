package relay

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"
)

func TestWindowBlocksUntilGranted(t *testing.T) {
	w := newWindow(10)
	if a, err := w.wait(context.Background()); err != nil || a != 10 {
		t.Fatalf("%d %v", a, err)
	}
	w.take(10)
	got := make(chan int64, 1)
	go func() {
		a, _ := w.wait(context.Background())
		got <- a
	}()
	select {
	case <-got:
		t.Fatal("wait returned with no window")
	case <-time.After(50 * time.Millisecond):
	}
	w.grant(7)
	if a := <-got; a != 7 {
		t.Fatalf("granted %d", a)
	}
	ctx, cancel := context.WithCancel(context.Background())
	w.take(7)
	cancel()
	if _, err := w.wait(ctx); err == nil {
		t.Fatal("wait ignored its context")
	}
	w.grant(maxWindow * 4)
	if a, _ := w.wait(context.Background()); a != maxWindow {
		t.Fatalf("window not clamped: %d", a)
	}
}

func TestInboxBoundedAndOrdered(t *testing.T) {
	b := newInbox(10)
	if b.push([]byte("abcd")) != pushOK || b.push([]byte("efgh")) != pushOK {
		t.Fatal("push")
	}
	if b.push([]byte("ijk")) != pushOverWindow {
		t.Fatal("11 bytes accepted in a 10-byte window")
	}
	b.pushEnd()
	if b.push([]byte("x")) != pushAfterEnd {
		t.Fatal("data accepted after end")
	}
	ctx := context.Background()
	for _, want := range []string{"abcd", "efgh"} {
		p, end, err := b.pop(ctx)
		if err != nil || end || string(p) != want {
			t.Fatalf("pop %q %v %v", p, end, err)
		}
	}
	if _, end, _ := b.pop(ctx); !end {
		t.Fatal("end not after the chunks")
	}
}

func TestDialErrorIsShort(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := ln.Addr().String()
	_ = ln.Close()
	_, err := net.DialTimeout("tcp", addr, time.Second)
	if got := DialError(err); got != "connection refused" {
		t.Fatalf("%q from %v", got, err)
	}
	if got := DialError(context.DeadlineExceeded); got != "timeout" {
		t.Fatal(got)
	}
	if got := DialError(errors.New("dial tcp 1.2.3.4:5: something odd")); got != "something odd" {
		t.Fatal(got)
	}
}

func TestStatsTargetErrors(t *testing.T) {
	var s Stats
	if m := s.TargetErrors(); m == nil || len(m) != 0 {
		t.Fatal("want an empty, non-nil map")
	}
	s.SetTargetError("bt_1:554", "timeout", false)
	s.SetTargetError("bt_1:80", "connection refused", false)
	s.SetTargetError("bt_1:554", "", true)
	if m := s.TargetErrors(); len(m) != 1 || m["bt_1:80"] != "connection refused" {
		t.Fatalf("%v", m)
	}
}
