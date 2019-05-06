import io

from flask import jsonify, send_file, request
from flask_login import login_required
from flask_restplus import Namespace, Resource, reqparse
from werkzeug.datastructures import FileStorage

from ..models import *
from ..util import query_util, coco_util

api = Namespace('image', description='Image related operations')

image_all = reqparse.RequestParser()
image_all.add_argument('fields', required=False, type=str)
image_all.add_argument('page', default=1, type=int)
image_all.add_argument('perPage', default=50, type=int, required=False)
image_upload = reqparse.RequestParser()
image_upload.add_argument('image', location='files',
                          type=FileStorage, required=True,
                          help='PNG or JPG file')
image_upload.add_argument('folder', required=False, default='',
                          help='Folder to insert photo into')

image_download = reqparse.RequestParser()
image_download.add_argument('asAttachment', type=bool, required=False, default=False)
image_download.add_argument('width', type=int, required=False, default=0)
image_download.add_argument('height', type=int, required=False, default=0)

copy_annotations = reqparse.RequestParser()
copy_annotations.add_argument('category_ids', location='json', type=list,
                              required=False, default=None, help='Categories to copy')
image_chunk = reqparse.RequestParser()
image_chunk.add_argument('md5', type=str, required=True)
image_chunk.add_argument('chunkNumber', type=int, required=True)
image_chunk.add_argument('file', location='files', type=FileStorage, required=True)

image_merge = reqparse.RequestParser()
image_merge.add_argument('file_name', type=str, required=True)
image_merge.add_argument('md5', type=str, required=True)


@api.route('/')
class Images(Resource):

    @api.expect(image_all)
    @login_required
    def get(self):
        """ Returns all images """
        args = image_all.parse_args()
        per_page = args['perPage']
        page = args['page'] - 1
        fields = args.get('fields', '')

        images = current_user.images.filter(deleted=False)
        total = images.count()
        pages = int(total / per_page) + 1

        images = images.skip(page * per_page).limit(per_page)
        if fields:
            images = images.only(*fields.split(','))

        return {
            "total": total,
            "pages": pages,
            "page": page,
            "fields": fields,
            "per_page": per_page,
            "images": query_util.fix_ids(images.all())
        }

    @api.expect(image_upload)
    @login_required
    def post(self):
        """ Creates an image """
        args = image_upload.parse_args()
        image = args['image']

        folder = args['folder']
        if len(folder) > 0:
            folder = folder[0].strip('/') + folder[1:]

        directory = os.path.join(Config.DATASET_DIRECTORY, folder)
        path = os.path.join(directory, image.filename)

        if os.path.exists(path):
            return {'message': 'file already exists'}, 400

        if not os.path.exists(directory):
            os.makedirs(directory)

        pil_image = Image.open(io.BytesIO(image.read()))

        image_model = ImageModel(
            file_name=image.filename,
            width=pil_image.size[0],
            height=pil_image.size[1],
            path=path
        )

        image_model.save()
        pil_image.save(path)

        image.close()
        pil_image.close()
        return query_util.fix_ids(image_model)


@api.route('/chunk')
class ChunkImage(Resource):
    def get(self):
        """
        检验该文件是否上传过
        :return:
        """
        md5 = request.args.get('md5', '')
        if not md5:
            return {'message': 'No md5!'}, 400

    @api.expect(image_chunk)
    # @login_required
    def post(self):
        """
        接收前端上传的每一个分片
        :return:
        """
        args = image_chunk.parse_args()
        md5 = args.get('md5')
        chunkNumber = args.get('chunkNumber')
        chunk_file = args.get('file')
        file_name = '{}-{}'.format(md5, chunkNumber)
        upload_path = Config.WEB_UPLOAD_DIRECTORY
        if not os.path.isdir(upload_path):
            os.makedirs(upload_path)
        chunk_file.save(os.path.join(upload_path, file_name))
        return jsonify({'result': 'success', 'needMerge': True, 'message': 'merge error!'})


@api.route('/merge-chunk')
class MergeChunk(Resource):
    @api.expect(image_merge)
    @login_required
    def post(self):
        """
        合并前端上传的文件
        :return:
        """
        args = image_merge.parse_args()
        fileName = args.get('file_name')
        md5 = args.get('md5')
        chunk = 1
        upload_path = Config.WEB_UPLOAD_DIRECTORY
        upload_file_path = os.path.join(upload_path, fileName)
        with open(upload_file_path, 'wb') as target_file:
            while True:
                try:
                    chunk_path = os.path.join(upload_path, '{}-{}'.format(md5, chunk))
                    source_file = open(chunk_path, 'rb')
                    target_file.write(source_file.read())
                    source_file.close()
                except IOError:
                    break
                chunk += 1
                # 删除该分片，节约资源
                os.remove(chunk_path)
        return jsonify({'result': '上传成功'})


@api.route('/<int:image_id>')
class ImageId(Resource):

    @login_required
    def get(self, image_id):
        """
        返回单个image对象
        :param image_id:
        :return:
        """
        return ''

    @login_required
    def delete(self, image_id):
        """ Deletes an image by ID """
        image = current_user.images.filter(id=image_id).first()
        if image is None:
            return {"message": "Invalid image id"}, 400
        image.delete()
        return {"success": True}


@api.route('/copy/<int:from_id>/<int:to_id>/annotations')
class ImageCopyAnnotations(Resource):

    @api.expect(copy_annotations)
    @login_required
    def post(self, from_id, to_id):
        args = copy_annotations.parse_args()
        category_ids = args.get('category_ids')

        image_from = current_user.images.filter(id=from_id).first()
        image_to = current_user.images.filter(id=to_id).first()

        if image_from is None or image_to is None:
            return {'success': False, 'message': 'Invalid image ids'}, 400

        if image_from == image_to:
            return {'success': False, 'message': 'Cannot copy self'}, 400

        if image_from.width != image_to.width or image_from.height != image_to.height:
            return {'success': False, 'message': 'Image sizes do not match'}, 400

        if category_ids is None:
            category_ids = DatasetModel.objects(id=image_from.dataset_id).first().categories

        query = AnnotationModel.objects(
            image_id=image_from.id,
            category_id__in=category_ids,
            deleted=False
        )

        return {'annotations_created': image_to.copy_annotations(query)}


@api.route('/<int:image_id>/thumbnail')
class ImageThumbnail(Resource):

    @api.expect(image_download)
    @login_required
    def get(self, image_id):
        """
        获取图片缩略图
        :param image_id:
        :return:
        """
        args = image_download.parse_args()
        as_attachment = args['asAttachment']
        width = args['width']
        height = args['height']

        image = current_user.images.filter(id=image_id, deleted=False).first()

        if image is None:
            return {'success': False}, 400

        if image.file_type == 'dzi':
            return

        if width < 1:
            width = image.width

        if height < 1:
            height = image.height

        pil_image = image.thumbnail()
        pil_image.thumbnail((width, height), Image.ANTIALIAS)

        image_io = io.BytesIO()
        pil_image = pil_image.convert("RGB")
        pil_image.save(image_io, "JPEG", quality=100)
        image_io.seek(0)

        return send_file(image_io, attachment_filename=image.file_name, as_attachment=as_attachment)


@api.route('/<int:image_id>/coco')
class ImageCoco(Resource):

    @login_required
    def get(self, image_id):
        """ Returns coco of image and annotations """
        image = current_user.images.filter(id=image_id).exclude('deleted_date').first()

        if image is None:
            return {"message": "Invalid image ID"}, 400

        if not current_user.can_download(image):
            return {"message": "You do not have permission to download the images's annotations"}, 403

        return coco_util.get_image_coco(image)
