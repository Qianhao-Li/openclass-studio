"""Finite top-left PDF geometry, independent of a rendering engine."""
import math

class Point(tuple):

    def __new__(cls, x, y):
        return super().__new__(cls, (float(x), float(y)))
    x = property(lambda self: self[0])
    y = property(lambda self: self[1])

class Rect(tuple):

    def __new__(cls, *args):
        values = tuple(args[0]) if len(args) == 1 else args or (0, 0, 0, 0)
        if len(values) != 4 or any((not math.isfinite(float(value)) for value in values)):
            raise ValueError('A PDF rectangle requires four finite coordinates')
        return super().__new__(cls, map(float, values))
    x0 = property(lambda self: self[0])
    y0 = property(lambda self: self[1])
    x1 = property(lambda self: self[2])
    y1 = property(lambda self: self[3])
    width = property(lambda self: max(0.0, self.x1 - self.x0))
    height = property(lambda self: max(0.0, self.y1 - self.y0))
    is_empty = property(lambda self: self.width <= 0 or self.height <= 0)
    is_valid = property(lambda self: self.x0 <= self.x1 and self.y0 <= self.y1)
    tl = property(lambda self: Point(self.x0, self.y0))
    br = property(lambda self: Point(self.x1, self.y1))

    def contains(self, other):
        if len(other) == 2:
            return self.x0 <= other[0] <= self.x1 and self.y0 <= other[1] <= self.y1
        return self.x0 <= other[0] and self.y0 <= other[1] and (self.x1 >= other[2]) and (self.y1 >= other[3])

    def intersects(self, other):
        return max(self.x0, other[0]) < min(self.x1, other[2]) and max(self.y0, other[1]) < min(self.y1, other[3])

    def __and__(self, other):
        return Rect(max(self.x0, other[0]), max(self.y0, other[1]), min(self.x1, other[2]), min(self.y1, other[3]))

    def __or__(self, other):
        return Rect(min(self.x0, other[0]), min(self.y0, other[1]), max(self.x1, other[2]), max(self.y1, other[3]))

    def get_area(self):
        return self.width * self.height

class Matrix:

    def __init__(self, a, d):
        self.a = float(a)
        self.d = float(d)
