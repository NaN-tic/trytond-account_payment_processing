# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction


class Move(metaclass=PoolMeta):
    __name__ = 'account.move'

    @classmethod
    def post(cls, moves):
        pool = Pool()
        Payment = pool.get('account.payment')

        if Transaction().context.get('processing_move_post'):
            return super(Move, cls).post(moves)

        result = super(Move, cls).post(moves)

        to_post = []
        for move in moves:
            origin = move.origin
            if isinstance(origin, Payment):
                payment = origin
                if (payment.processing_move
                        and payment.processing_move.state == 'draft'):
                    to_post.append(payment.processing_move)

        if to_post:
            unique = {move.id: move for move in to_post}
            with Transaction().set_context(processing_move_post=True):
                cls.post(list(unique.values()))

        return result
