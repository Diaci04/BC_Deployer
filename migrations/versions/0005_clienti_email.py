"""clienti e notifiche email

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 15:00:00
"""
from alembic import op
import sqlalchemy as sa
import app.models


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('customer', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('notify_emails', sa.Text(), nullable=True))
    op.create_table('smtp_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('host', sa.String(length=255), nullable=False),
    sa.Column('port', sa.Integer(), nullable=False),
    sa.Column('security', sa.String(length=10), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=True),
    sa.Column('password_enc', sa.Text(), nullable=True),
    sa.Column('from_addr', sa.String(length=255), nullable=False),
    sa.Column('updated_at', app.models.UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('smtp_settings')
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.drop_column('notify_emails')
        batch_op.drop_column('customer')
