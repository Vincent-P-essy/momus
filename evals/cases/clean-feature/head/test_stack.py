from stack import Stack


def test_push_peek_pop():
    stack = Stack()
    stack.push(1)
    stack.push(2)
    assert stack.peek() == 2
    assert stack.pop() == 2
    assert stack.pop() == 1
    assert stack.is_empty()
