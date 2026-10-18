package tunnel

import (
	"context"
	"errors"
	"sync"

	"github.com/coder/websocket"

	"axiomvision.ai/bridge/internal/proto"
)

// queue feeds the connection's one writer goroutine. Two lists: priority (credit, ping/pong, heartbeat), which may
// jump ahead, and ordered (stream data and every other control frame), which keeps an `end` or `abort` behind the
// stream's last chunk. Control pushes never block, so the reader can always answer; data pushes block while queued
// data is over the memory cap, which stops the camera pumps (and, through TCP, the cameras) when the uplink is slow.
type queue struct {
	mu        sync.Mutex
	pri       []item
	ord       []item
	dataBytes int64
	ctrl      int // control frames waiting (both lists)
	err       error
	wake      chan struct{} // writer: something was queued
	space     chan struct{} // data producers: closed (and replaced) when queued data shrinks
	dataCap   int64
	ctrlCap   int
}

type item struct {
	typ  websocket.MessageType
	b    []byte
	data bool
}

var (
	errQueueClosed   = errors.New("connection closed")
	errQueueOverflow = errors.New("too many control frames queued (the hub is not reading)")
)

const (
	defaultDataCap = 4 << 20 // bytes of chunks waiting for the socket, all streams together
	defaultCtrlCap = 4096
)

func newQueue() *queue {
	return &queue{wake: make(chan struct{}, 1), space: make(chan struct{}), dataCap: defaultDataCap, ctrlCap: defaultCtrlCap}
}

func (q *queue) signal() {
	select {
	case q.wake <- struct{}{}:
	default:
	}
}

func (q *queue) pushControl(frame any, priority bool) {
	b, err := proto.Encode(frame)
	if err != nil {
		return // only our own structs come here; none fails to encode
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.err != nil {
		return
	}
	if q.ctrl >= q.ctrlCap {
		q.err = errQueueOverflow
		q.signal()
		return
	}
	it := item{typ: websocket.MessageText, b: b}
	if priority {
		q.pri = append(q.pri, it)
	} else {
		q.ord = append(q.ord, it)
	}
	q.ctrl++
	q.signal()
}

// Ordered implements relay.Outbox.
func (q *queue) Ordered(frame any) { q.pushControl(frame, false) }

// Priority implements relay.Outbox.
func (q *queue) Priority(frame any) { q.pushControl(frame, true) }

// Data implements relay.Outbox: queue a binary frame, waiting while queued data is over the cap.
func (q *queue) Data(ctx context.Context, b []byte) error {
	for {
		q.mu.Lock()
		if q.err != nil {
			q.mu.Unlock()
			return q.err
		}
		if q.dataBytes == 0 || q.dataBytes+int64(len(b)) <= q.dataCap {
			q.ord = append(q.ord, item{typ: websocket.MessageBinary, b: b, data: true})
			q.dataBytes += int64(len(b))
			q.signal()
			q.mu.Unlock()
			return nil
		}
		space := q.space
		q.mu.Unlock()
		select {
		case <-space:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// next is the writer's side: priority frames first, then the ordered list.
func (q *queue) next(ctx context.Context) (item, error) {
	for {
		q.mu.Lock()
		if q.err != nil {
			err := q.err
			q.mu.Unlock()
			return item{}, err
		}
		var it item
		ok := true
		switch {
		case len(q.pri) > 0:
			it, q.pri[0] = q.pri[0], item{}
			q.pri = q.pri[1:]
		case len(q.ord) > 0:
			it, q.ord[0] = q.ord[0], item{}
			q.ord = q.ord[1:]
		default:
			ok = false
		}
		if ok {
			if it.data {
				q.dataBytes -= int64(len(it.b))
				close(q.space)
				q.space = make(chan struct{})
			} else {
				q.ctrl--
			}
			q.mu.Unlock()
			return it, nil
		}
		q.mu.Unlock()
		select {
		case <-q.wake:
		case <-ctx.Done():
			return item{}, ctx.Err()
		}
	}
}

// close fails every later push and wakes everyone waiting.
func (q *queue) close() {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.err == nil {
		q.err = errQueueClosed
	}
	close(q.space)
	q.space = make(chan struct{})
	q.signal()
}
